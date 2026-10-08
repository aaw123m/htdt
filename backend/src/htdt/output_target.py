"""Operator-chosen export target validation (#984).

One typed rule for directories a file chooser hands to a package
builder, shared by every package export surface. The check it replaces
— ``target.startswith(('/', 'C:', 'D:'))`` — rejected valid ``E:/``
``F:/`` and UNC locations and passed ``C:relative`` drive-relative
spellings that are not absolute on Windows at all.

The rule is ``Path``-based and resolved against the real filesystem:

- the spelled path must be Windows-absolute — any drive letter
  ``A:``–``Z:`` (case-insensitive), a ``\\\\server\\share`` UNC root,
  or an extended ``\\\\?\\`` form; drive-relative (``C:foo``),
  driveless (``/foo``) and bare names fail;
- the drive or share must actually be reachable — a disconnected
  ``E:`` or an offline share is a named failure, not a silent miss;
- the chosen directory must exist, be a directory, and be writable —
  writability is probed with a real create/delete, never inferred;
- no component of the spelled path may be a symlink/junction, and the
  package directory itself may not be a link — the package must land
  exactly where the operator chose, never silently outside it;
- the package directory must not already hold artifacts (no automatic
  overwrite) and must fit the legacy ``MAX_PATH`` budget.

Every rejection raises :class:`OutputTargetError` carrying a stable
machine ``reason`` plus an operator-facing suggestion; the caller shows
both and never falls back to a different location.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import uuid

#: Conservative length budget for the package directory, in characters.
#: Legacy Win32 MAX_PATH is 260 and package members add relative names
#: up to ~50 chars (``renders/<slug>-yaw-120.png`` and similar), so the
#: package root must stay well under it — long-path opt-in manifests
#: cannot be assumed on operator machines.
_PACKAGE_DIR_LIMIT = 200


@dataclass(frozen=True)
class OutputTarget:
    """A validated, operator-chosen output location."""

    root: Path
    """The directory the operator chose — resolved, existing, writable."""

    package_dir: Path
    """``root / package_name`` — the exact directory the build writes."""


class OutputTargetError(ValueError):
    """A rejected output target — named reason + operator suggestion.

    ``reason`` is the stable machine name tests and diagnostics key on;
    ``str(self)`` is the localized operator-facing detail and
    ``suggestion`` the concrete alternative offered alongside it.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.suggestion = suggestion

    def operator_text(self) -> str:
        """Reason and suggestion as one localized warning body."""
        if self.suggestion:
            return f'{self}\n{self.suggestion}'
        return str(self)


def _is_unc_drive(drive: str) -> bool:
    """``\\\\server\\share`` style drives, excluding ``\\\\?\\``/``\\\\.\\``
    extended *local* device spellings."""
    return drive.startswith('\\\\') and not drive.startswith(
        ('\\\\?\\', '\\\\.\\')
    )


def _reparse_component(path: Path) -> Path | None:
    """First component of ``path`` that is a symlink or junction.

    Walks the spelled (unresolved) path so a link anywhere in the chain
    is caught — checking only the leaf misses ``C:\\link\\sub`` where
    ``C:\\link`` is the junction. 8.3 short-name spellings (``ADMINI~1``)
    are not reparse points and correctly pass.
    """
    for current in (path, *path.parents):
        try:
            if current.is_symlink() or current.is_junction():
                return current
        except OSError:
            return current
    return None


def _assert_writable(root: Path) -> None:
    """Prove ``root`` accepts writes by creating and removing a file.

    ``os.access`` on Windows answers from the access token, not the
    directory's real ACL — a genuine create/delete probe is the only
    honest check for read-only media and denied shares. It is written
    as a direct ``os.open`` — ``tempfile.mkstemp`` retries forever on
    ACL-denied directories (its Windows ``PermissionError`` branch
    re-checks ``os.access``, which still reports writable) and would
    hang the dialog instead of naming the failure.
    """
    probe = root / f'.htdt-target-{os.getpid()}-{uuid.uuid4().hex}.tmp'
    fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(fd)
    finally:
        probe.unlink(missing_ok=True)


def validate_output_target(
    raw: str | os.PathLike[str] | None, *, package_name: str
) -> OutputTarget:
    """Validate an operator-chosen output root for a package build.

    Returns the checked :class:`OutputTarget`; raises
    :class:`OutputTargetError` with a named ``reason`` and an operator
    ``suggestion`` on any failure. Never touches the filesystem outside
    the chosen root and never silently substitutes another location.
    """
    text = '' if raw is None else str(raw).strip().strip('"').strip()
    if not text:
        raise OutputTargetError(
            'empty',
            '出力先が選択されていません',
            suggestion='「出力先を選択」で保存先フォルダを選んでください。',
        )

    spelled = PureWindowsPath(text)
    if not spelled.is_absolute():
        raise OutputTargetError(
            'not_absolute',
            f'出力先が絶対パスではありません: {text}',
            suggestion=(
                'ドライブレター付きの絶対パス（例: C:\\出力）または '
                'UNC パス（\\\\server\\share\\出力）を選択してください。'
            ),
        )

    # Characters Windows can never store: <>|?* inside the tail, ':'
    # after the drive prefix (ADS spellings cannot name a directory),
    # and control bytes. Without this they surface as a misleading
    # "missing" later on.
    tail = text[len(spelled.drive):] if spelled.drive else text
    if (
        any(ch in tail for ch in '<>|?*:')
        or '\x00' in text
    ):
        raise OutputTargetError(
            'invalid_chars',
            f'出力先に使用できない文字を含みます: {text}',
            suggestion='<>|?*: を除いたフォルダパスを選択してください。',
        )

    path = Path(text)
    anchor_ok: bool
    # Reachability probes can raise as well as answer False — a
    # non-filesystem share (e.g. IPC$) errors with WinError 87 rather
    # than returning False. Either way the target is not usable, and a
    # half-dead network path must never be mistaken for 'missing'.
    if _is_unc_drive(spelled.drive):
        try:
            anchor_ok = Path(spelled.anchor).exists()
        except OSError:
            anchor_ok = False
        if not anchor_ok:
            raise OutputTargetError(
                'share_unavailable',
                f'ネットワーク共有に接続できません: {spelled.drive}',
                suggestion=(
                    'サーバー・共有への接続を確認するか、ローカルの'
                    '出力先を選択してください。'
                ),
            )
    elif spelled.drive:
        try:
            anchor_ok = Path(f'{spelled.drive}\\').exists()
        except OSError:
            anchor_ok = False
        if not anchor_ok:
            raise OutputTargetError(
                'drive_unavailable',
                f'ドライブ {spelled.drive} が見つかりません（未接続の可能性があります）',
                suggestion='ドライブを接続するか、別の出力先を選択してください。',
            )

    reparse = _reparse_component(path)
    if reparse is not None:
        try:
            real = str(path.resolve())
        except (OSError, RuntimeError):
            real = '不明'
        raise OutputTargetError(
            'redirected',
            f'出力先はリンク/ジャンクション経由で別の場所へ転送されます: '
            f'{reparse} → {real}',
            suggestion=(
                'リンクではなく実在のフォルダを直接選択してください'
                f'（実体: {real}）。'
            ),
        )

    try:
        path_exists = path.exists()
        path_is_dir = path_exists and path.is_dir()
    except OSError as exc:
        raise OutputTargetError(
            'io_error',
            f'出力先を確認できませんでした: {exc}',
            suggestion='出力先の状態を確認してから再試行してください。',
        ) from exc
    if not path_exists:
        raise OutputTargetError(
            'missing',
            f'出力先フォルダが存在しません: {path}',
            suggestion='フォルダを作成してから再試行するか、既存の場所を選択してください。',
        )
    if not path_is_dir:
        raise OutputTargetError(
            'not_directory',
            f'出力先がフォルダではありません: {path}',
            suggestion='ファイルではなくフォルダを選択してください。',
        )

    package_dir = path / package_name
    if package_dir.is_symlink() or package_dir.is_junction():
        raise OutputTargetError(
            'link_escape',
            f'出力先 {package_dir.name} はリンク/ジャンクションです: {package_dir}',
            suggestion='リンクを削除するか、別の出力先を選択してください。',
        )
    if package_dir.exists():
        if not package_dir.is_dir():
            raise OutputTargetError(
                'not_directory',
                f'出力先に同名のファイルがあります: {package_dir}',
                suggestion='そのファイルを移動するか、別の出力先を選択してください。',
            )
        if any(package_dir.iterdir()):
            raise OutputTargetError(
                'collision',
                f'出力先に既存の生成物があります: {package_dir}',
                suggestion=(
                    '既存フォルダを退避/削除するか、別の出力先を選択してください'
                    '（上書きは行いません）。'
                ),
            )
    if len(str(package_dir)) > _PACKAGE_DIR_LIMIT:
        raise OutputTargetError(
            'path_too_long',
            f'出力先パスが長すぎます（{len(str(package_dir))} 文字）: {package_dir}',
            suggestion='より浅い階層の出力先を選択してください。',
        )

    try:
        _assert_writable(path)
    except PermissionError as exc:
        if _is_unc_drive(spelled.drive):
            raise OutputTargetError(
                'share_denied',
                f'ネットワーク共有への書き込みが拒否されました: {path}',
                suggestion='共有の権限を確認するか、ローカルの出力先を選択してください。',
            ) from exc
        raise OutputTargetError(
            'not_writable',
            f'出力先に書き込み権限がありません: {path}',
            suggestion='書き込み可能なフォルダを選択するか、権限を確認してください。',
        ) from exc
    except OSError as exc:
        raise OutputTargetError(
            'io_error',
            f'出力先を確認できませんでした: {exc}',
            suggestion='出力先の状態を確認してから再試行してください。',
        ) from exc

    return OutputTarget(root=path, package_dir=package_dir)


__all__ = [
    'OutputTarget',
    'OutputTargetError',
    'validate_output_target',
]
