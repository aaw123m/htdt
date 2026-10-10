"""Typed error-boundary vocabulary for the native workflow UI (#815).

Broad ``except Exception`` catches at UI edges are classified by *product
semantics*, not rewritten mechanically. This module defines the taxonomy:

- ``EXPECTED_OPERATION_ERRORS`` — the tuple UI edges catch instead of
  ``Exception``. It covers every failure the product already treats as an
  expected, recoverable operation rejection: domain validation and
  authority errors (all ``ValueError`` subclasses: ``*IntegrityError``,
  ``*StaleHeadError``, ``*ConflictError``, ``MeasurementWorkflowError``,
  parse/validation rejections), missing-key reads (``KeyError``), adapter
  I/O (``OSError``, ``sqlite3.Error`` store access, pydantic input
  validation), and the ``RuntimeError`` family the domain raises for
  operational failures (``EditStateError``, ``RewApiError``,
  ``NativeSchemaError``, ``ManagedDataUnavailableError``).

  Anything outside this tuple — ``TypeError``, ``AttributeError``,
  ``IndexError``, ``AssertionError`` — is an unexpected programming error
  (category 5). It must NOT masquerade as a recoverable rejection, so it
  is deliberately not caught here: it propagates to the process's single
  uncaught-exception boundary (``native_diagnostics.push_uncaught_sink``
  via ``sys.excepthook``), which records it in the Activity Center and
  shows a non-recoverable status notice.

- ``is_authority_failure`` — category-4 detection. Every persistence /
  authority / integrity / stale-state exception in the sealed repository
  is a ``ValueError`` subclass, so *type* narrowing cannot separate it
  from user input. Classification therefore follows the leaf-suffix
  convention already used by ``user_facing_error`` — ``*IntegrityError``,
  ``*StaleHeadError``, ``*ConflictError``, ``*RefError``, ``*SealError``,
  ``*TamperError``, ``*SchemaError`` — plus known runtime authority types
  and hard sqlite failure codes. At a degraded-read site an authority
  failure must never become a falsified default (``()``, ``'current'``,
  step-1): the call site re-raises so the failure stays observable:

      except EXPECTED_OPERATION_ERRORS as exc:
          if is_authority_failure(exc):
              raise
          report_boundary_failure(exc, operation='...')
          values = ()

- ``report_boundary_failure`` — observability for a degraded read: the
  fallback keeps the design intent (an unreadable list shows empty,
  never fabricated rows) while the failure is classified and logged —
  authority failures at ERROR with traceback, expected operational
  failures through the standard ``log_operation_error`` WARNING path.

- ``ERROR_BOUNDARY_MARKER`` — a broad catch that *legitimately* stays
  broad carries ``# error-boundary: <category> — <why>`` on its
  ``except`` line: teardown/reporting boundaries that must absorb every
  failure type (cleanup before re-raise, the uncaught-exception recorder
  itself, shutdown accounting). The guard test in
  ``test_issue_815_error_boundaries.py`` fails if any broad catch lacks
  the marker.

Category 3 (cancellation/teardown) failures are covered by the same rule:
cleanup boundaries stay broad with the marker and always re-raise so the
original failure is never masked.
"""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from enum import Enum
from typing import Final

from pydantic import ValidationError

from .ui_theme_tokens import SemanticState
from .user_facing_error import (
    UserFacingError,
    log_operation_error,
    to_user_facing_error,
)

_LOG = logging.getLogger('htdt.errors')

#: Comment marker for a deliberately broad catch (see module docstring).
ERROR_BOUNDARY_MARKER: Final[str] = 'error-boundary:'


class BoundaryCategory(str, Enum):
    """#815 classification taxonomy for a caught exception."""

    #: Expected user/input or domain rejection → actionable UI feedback.
    USER_INPUT = 'user_input'
    #: External dependency/adapter failure → preserve exact reason.
    EXTERNAL_ADAPTER = 'external_adapter'
    #: Cancellation/teardown/cleanup → must not mask the original failure.
    TEARDOWN = 'teardown'
    #: Persistence/authority/integrity/stale-state → fail closed.
    AUTHORITY = 'authority_persistence'
    #: Unexpected programming error → central diagnostics, not recoverable.
    UNEXPECTED = 'unexpected'


#: Failures the product treats as expected operation rejections. See the
#: module docstring; unexpected errors are intentionally excluded.
EXPECTED_OPERATION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    ValueError,
    KeyError,
    OSError,
    RuntimeError,
    _sqlite3.Error,
    ValidationError,
)

#: Leaf-name suffixes of sealed-authority/integrity failures — the whole
#: family hangs off ``ValueError``, so classification is by name, matching
#: the ``user_facing_error`` mapping convention.
_AUTHORITY_NAME_SUFFIXES: Final[tuple[str, ...]] = (
    'IntegrityError',
    'StaleHeadError',
    'ConflictError',
    'RefError',
    'SealError',
    'TamperError',
    'SchemaError',
)

#: Authority-family runtime types (RuntimeError subclasses whose MRO
#: membership identifies a persistence/authority failure even when the
#: leaf name lacks a suffix).
_AUTHORITY_MRO_NAMES: Final[frozenset[str]] = frozenset(
    {
        'NativeSchemaError',
        'NativeUpgradeError',
        'ManagedDataUnavailableError',
    }
)

#: sqlite failures whose continued degradation could hide a corrupted or
#: unreadable sealed store — always treated as authority failures even
#: though ``sqlite3.Error`` is in ``EXPECTED_OPERATION_ERRORS``.
_AUTHORITY_SQLITE_CODES: Final[frozenset[int]] = frozenset(
    {
        _sqlite3.SQLITE_CORRUPT,
        _sqlite3.SQLITE_NOTADB,
        _sqlite3.SQLITE_CANTOPEN,
        _sqlite3.SQLITE_READONLY,
    }
)

#: Builtin exception families that are always programming errors.
_UNEXPECTED_BUILTIN_TYPES: Final[tuple[type[BaseException], ...]] = (
    TypeError,
    AttributeError,
    IndexError,
    NameError,
    AssertionError,
    ArithmeticError,
)

#: External adapter families — dependency, device, file/import, network.
_EXTERNAL_MRO_NAMES: Final[frozenset[str]] = frozenset(
    {
        'RewApiError',
        'RewApiUnavailable',
        'RewApiNotFound',
        'RewApiCancelledError',
        'RewApiResponseTooLarge',
        'RewParseError',
        'RewIrParseError',
        'UnderlayImportError',
        'RawMeshImportError',
        'CaptureImportError',
        'MeshAssetUnavailableError',
        'IngressTooLargeError',
        'BackupError',
    }
)

#: Expected user/input rejections — domain errors that are neither
#: authority nor adapter failures.
_USER_INPUT_MRO_NAMES: Final[frozenset[str]] = frozenset(
    {
        'EditStateError',
        'SceneValidationError',
        'OperationTransitionError',
        'PreferenceError',
        'LibraryError',
        'ProjectLibraryError',
    }
)


def is_authority_failure(exc: BaseException) -> bool:
    """True when ``exc`` is a persistence/authority/integrity failure.

    These must stay observable and fail closed: a caller-facing degrade
    (``()``, ``None``, ``'current'``) must never swallow them.
    """
    name = type(exc).__name__
    if name.endswith(_AUTHORITY_NAME_SUFFIXES):
        return True
    mro_names = {klass.__name__ for klass in type(exc).__mro__}
    if mro_names & _AUTHORITY_MRO_NAMES:
        return True
    if isinstance(exc, _sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', 0) & 0xFF
        return code in _AUTHORITY_SQLITE_CODES
    return False


def classify_boundary_error(exc: BaseException) -> BoundaryCategory:
    """Map one caught exception onto the boundary taxonomy."""
    if is_authority_failure(exc):
        return BoundaryCategory.AUTHORITY
    if isinstance(exc, _UNEXPECTED_BUILTIN_TYPES):
        return BoundaryCategory.UNEXPECTED
    mro_names = {klass.__name__ for klass in type(exc).__mro__}
    if mro_names & _EXTERNAL_MRO_NAMES:
        return BoundaryCategory.EXTERNAL_ADAPTER
    if isinstance(exc, (OSError, _sqlite3.Error)):
        return BoundaryCategory.EXTERNAL_ADAPTER
    if mro_names & _USER_INPUT_MRO_NAMES:
        return BoundaryCategory.USER_INPUT
    if isinstance(exc, (ValueError, KeyError, ValidationError)):
        return BoundaryCategory.USER_INPUT
    if isinstance(exc, RuntimeError):
        # Domain RuntimeErrors not named above are operational failures
        # raised by adapters/infra (e.g. Qt C++ object lifetime).
        return BoundaryCategory.EXTERNAL_ADAPTER
    return BoundaryCategory.UNEXPECTED


def report_unexpected_error(
    exc: BaseException,
    *,
    operation: str,
    logger: logging.Logger | None = None,
) -> UserFacingError:
    """Category-5 boundary: record an unexpected error in diagnostics.

    Logs at ERROR with traceback on the diagnostics channel (persisted by
    the diagnostics handler) and returns a stable, non-recoverable-looking
    ``UserFacingError`` — same '予期しないエラー' semantics as the
    uncaught-exception Activity Center path.
    """
    log = logger if logger is not None else _LOG
    log.error(
        'unexpected error during %s: %s: %s',
        operation,
        type(exc).__name__,
        exc,
        exc_info=exc,
    )
    return UserFacingError(
        code='internal.unexpected',
        severity=SemanticState.ERROR,
        title=operation,
        message='予期しない問題が発生しました',
        effect='操作は完了していません',
        recovery='詳細は診断ログに記録されました',
        technical_detail=f'{type(exc).__name__}: {exc}',
    )


def report_boundary_failure(
    exc: BaseException,
    *,
    operation: str,
    logger: logging.Logger | None = None,
) -> UserFacingError:
    """Make a degraded-read failure observable without changing the degrade.

    Classifies the failure: authority/integrity failures log at ERROR
    (the sealed store is suspect — a silent empty state would hide it),
    unexpected errors go through :func:`report_unexpected_error`, and
    expected operational failures log at WARNING via the standard
    ``log_operation_error`` path. The caller still applies its designed
    fallback; the failure is simply never silent.
    """
    category = classify_boundary_error(exc)
    if category is BoundaryCategory.UNEXPECTED:
        return report_unexpected_error(exc, operation=operation, logger=logger)
    log = logger if logger is not None else _LOG
    error = to_user_facing_error(exc, title=operation)
    if category is BoundaryCategory.AUTHORITY:
        log.error(
            'authority failure during %s: %s: %s',
            operation,
            type(exc).__name__,
            exc,
            exc_info=exc,
        )
    else:
        log_operation_error(error, exc)
    return error
