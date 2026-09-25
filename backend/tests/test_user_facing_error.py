"""#903: user-facing error contract — mapping, codes, and detail preservation."""

from htdt.cad_layout_tools import LayoutError
from htdt.cad_design_decision_repository import DesignDecisionConflictError
from htdt.rew_parser import RewParseError
from htdt.user_facing_error import (
    operation_error_message,
    to_user_facing_error,
)


def test_unknown_exception_maps_to_safe_generic() -> None:
    error = to_user_facing_error(
        RuntimeError('sqlite3.OperationalError near line 4821'),
        title='測定を保存できませんでした',
    )
    assert error.code == 'operation.failed'
    assert error.message == '操作を完了できませんでした'
    # Raw backend text never becomes the primary message.
    assert 'sqlite3' not in error.message
    # …but is preserved for the details/diagnostics path.
    assert 'RuntimeError' in (error.technical_detail or '')
    assert '4821' in (error.technical_detail or '')


def test_rew_parse_maps_to_import_code_with_recovery() -> None:
    error = to_user_facing_error(
        RewParseError('unexpected token at line 3'),
        title='読み込みに失敗しました',
    )
    assert error.code == 'import.rew'
    assert error.recovery is not None
    assert 'REW' in error.message


def test_conflict_suffix_maps_to_stable_authority_code() -> None:
    error = to_user_facing_error(
        DesignDecisionConflictError('decision id already exists'),
        title='決定を登録できませんでした',
        effect='変更は保存されていません',
    )
    assert error.code == 'authority.conflict'
    assert error.recovery is not None
    text = error.notice_text()
    assert '決定を登録できませんでした' in text
    assert '変更は保存されていません' in text
    assert 'already exists' not in text


def test_user_facing_domain_message_is_preserved() -> None:
    exc = LayoutError('コピーする項目を選択してください')
    error = to_user_facing_error(exc, title='コピーできませんでした')
    assert error.message == 'コピーする項目を選択してください'
    assert error.code == 'operation.rejected'


def test_io_errors_get_typed_codes() -> None:
    assert to_user_facing_error(
        FileNotFoundError('x'), title='t'
    ).code == 'io.not_found'
    assert to_user_facing_error(
        PermissionError('x'), title='t'
    ).code == 'io.permission'
    assert to_user_facing_error(
        KeyError('missing-entity'), title='t'
    ).code == 'data.missing'


def test_operation_error_message_never_echoes_raw_text() -> None:
    message = operation_error_message(
        ValueError('field name "document_id" shadows an attribute')
    )
    assert 'document_id' not in message
    assert message == 'データを処理できませんでした'
