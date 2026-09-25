"""Support & Diagnostics Center backend (#604).

``native_diagnostics.py`` already owns the rotating, scrubbed log; this
module makes the *existing* capability reachable and adds the product-level
pieces:

* :func:`environment_summary` — build identity, frozen/package state,
  schema version, data directory, project identity, log location;
* :func:`run_health_checks` — bounded, non-mutating checks grouped into
  ``app_storage`` / ``semantic_integrity`` / ``integrations``; a failed
  optional integration downgrades only its own result, never the global
  verdict;
* :class:`DiagnosticPackageBuilder` — one bounded ZIP containing only
  diagnostic/support material, with a privacy preview of included
  categories before anything is written and a hard exclusion list for raw
  project/evidence/credential material;
* :func:`failure_correlation_id` — short local IDs (``[diag: 8F2A]``) that
  correlate a visible failure with its log record;
* :func:`previous_session_unexpected_end` — evidence-based detection of an
  unclean exit (a live ``runtime.json`` whose owning process is gone), never
  inferred from a log that merely stopped.

Boundaries kept explicit: *verify project data* = semantic integrity
(#426), *diagnostic package* = debugging information, *backup* = disaster
recovery, *project bundle* = portability. The package is never labelled a
backup or export.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .managed_assets import MANAGED_ASSETS_DIRNAME
from .native_diagnostics import (
    BuildIdentity,
    DIAGNOSTICS_DIRNAME,
    LOG_FILENAME,
    build_identity,
    diagnostics_dir,
)
from .runtime_instance import (
    LOCK_FILENAME,
    _lock_first_byte,
    _unlock_first_byte,
    read_lock_metadata,
    read_runtime_info,
)


SUPPORT_SCHEMA_VERSION = 1
DATABASE_NAME = 'cad-scenes.sqlite3'

#: A diagnostic package is deliberately small; anything larger is cut with a
#: manifest note rather than silently ballooning into a data export.
PACKAGE_BYTE_BUDGET = 25 * 1024 * 1024
PACKAGE_PREFIX = 'htdt-diagnostics'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace(
        '+00:00', 'Z'
    )


def failure_correlation_id(seed: str | None = None) -> str:
    """Short local diagnostic ID shown next to a visible failure.

    ``Prediction failed [diag: 8F2A]`` matches the same id inside the log
    and any diagnostic package — no stack traces in normal workflow.
    """

    if seed is None:
        return uuid.uuid4().hex[:4].upper()
    return sha256(seed.encode('utf-8')).hexdigest()[:4].upper()


# ---------------------------------------------------------------------------
# Environment / build summary


class EnvironmentSummary(BaseModel):
    """Build identity + install state for the Support surface."""

    model_config = ConfigDict(frozen=True)

    display_version: str
    build: str
    frozen: bool
    schema_version: int
    data_dir: str
    diagnostics_dir: str
    log_path: str | None
    project_ref: str | None = None


def environment_summary(
    data_dir: Path,
    *,
    project_ref: str | None = None,
    identity: BuildIdentity | None = None,
) -> EnvironmentSummary:
    identity = identity or build_identity()
    log_path = diagnostics_dir(Path(data_dir)) / LOG_FILENAME
    return EnvironmentSummary(
        display_version=f'HTDT {identity.version}' + (' (packaged)' if identity.frozen else ''),
        build=identity.describe(),
        frozen=identity.frozen,
        schema_version=_schema_version(),
        data_dir=str(data_dir),
        diagnostics_dir=str(diagnostics_dir(Path(data_dir))),
        log_path=str(log_path),
        project_ref=project_ref,
    )


def _schema_version() -> int:
    """Native schema version surfaced to support output."""

    from .database import SCHEMA_VERSION  # local import keeps module Qt/db-light

    return SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Health checks


class HealthStatus(StrEnum):
    PASS = 'pass'
    ATTENTION = 'attention'
    FAIL = 'fail'
    UNKNOWN = 'unknown'
    NOT_APPLICABLE = 'not_applicable'


class HealthCategory(StrEnum):
    APP_STORAGE = 'app_storage'
    SEMANTIC_INTEGRITY = 'semantic_integrity'
    INTEGRATIONS = 'integrations'


class HealthCheckResult(BaseModel):
    """One bounded, non-mutating check result."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    category: HealthCategory
    status: HealthStatus
    summary: str = Field(min_length=1)
    detail: str | None = None


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    checked_at: str
    results: tuple[HealthCheckResult, ...]

    @property
    def failed(self) -> tuple[HealthCheckResult, ...]:
        return tuple(r for r in self.results if r.status == HealthStatus.FAIL)

    def by_category(self, category: HealthCategory) -> tuple[HealthCheckResult, ...]:
        return tuple(r for r in self.results if r.category == category)

    @property
    def overall(self) -> HealthStatus:
        """Global status: optional-integration failures degrade to ATTENTION.

        A failed REW/Capture/GPU check must not brand the whole installation
        broken; only app-storage or integrity failures yield global FAIL.
        """

        statuses = {r.status for r in self.results}
        if HealthStatus.FAIL in {
            r.status
            for r in self.results
            if r.category != HealthCategory.INTEGRATIONS
        }:
            return HealthStatus.FAIL
        if statuses & {HealthStatus.FAIL, HealthStatus.ATTENTION}:
            return HealthStatus.ATTENTION
        if statuses == {HealthStatus.NOT_APPLICABLE} or not statuses:
            return HealthStatus.UNKNOWN
        if statuses <= {HealthStatus.PASS, HealthStatus.NOT_APPLICABLE}:
            return HealthStatus.PASS
        return HealthStatus.ATTENTION


def _check_database(data_dir: Path) -> list[HealthCheckResult]:
    """Two distinct storage findings (#749): openability and integrity.

    ``storage.database_openable`` says the file can be opened read-only;
    ``storage.sqlite_quick_check`` reports SQLite structural integrity and
    only passes on the canonical ``ok`` result row — a successful query
    with diagnostic rows is never a PASS.
    """

    path = Path(data_dir) / DATABASE_NAME
    if not path.exists():
        absent = HealthCheckResult(
            check_id='storage.database_openable',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.NOT_APPLICABLE,
            summary='project database not created yet',
            detail=str(path),
        )
        return [
            absent,
            absent.model_copy(
                update={'check_id': 'storage.sqlite_quick_check'}
            ),
        ]
    try:
        with sqlite3.connect(
            f'file:{path.as_posix()}?mode=ro', uri=True
        ) as conn:
            rows = [
                str(row[0])
                for row in conn.execute('PRAGMA quick_check(1)')
            ]
    except sqlite3.Error as exc:
        openable = HealthCheckResult(
            check_id='storage.database_openable',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.FAIL,
            summary='project database failed to open',
            detail=str(exc),
        )
        return [
            openable,
            HealthCheckResult(
                check_id='storage.sqlite_quick_check',
                category=HealthCategory.APP_STORAGE,
                status=HealthStatus.FAIL,
                summary='sqlite integrity could not be evaluated',
                detail=str(exc),
            ),
        ]
    results = [
        HealthCheckResult(
            check_id='storage.database_openable',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.PASS,
            summary='project database opens read-only',
            detail=str(path),
        )
    ]
    if rows == ['ok']:
        results.append(
            HealthCheckResult(
                check_id='storage.sqlite_quick_check',
                category=HealthCategory.APP_STORAGE,
                status=HealthStatus.PASS,
                summary='sqlite quick_check reports ok',
            )
        )
    else:
        results.append(
            HealthCheckResult(
                check_id='storage.sqlite_quick_check',
                category=HealthCategory.APP_STORAGE,
                status=HealthStatus.FAIL,
                summary='sqlite quick_check reported integrity problems',
                detail='; '.join(rows) if rows else 'no result row',
            )
        )
    return results


def _probe_lock_state(lock_path: Path) -> Literal['free', 'held', 'unknown']:
    """Probe the real OS byte-range lock authority for the data directory.

    Advisory owner metadata past byte 0 is never consulted here — only an
    actual non-blocking lock attempt answers whether another process holds
    the authoritative lock (#749). The file is never created by a probe.
    """

    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return 'free'
    except OSError:
        return 'unknown'
    try:
        with os.fdopen(fd, 'r+b', buffering=0) as file:
            try:
                _lock_first_byte(file)
            except OSError:
                return 'held'
            try:
                _unlock_first_byte(file)
            except OSError:
                pass
            return 'free'
    except OSError:
        return 'unknown'


def _check_lock(data_dir: Path, *, owns_lock: bool = False) -> HealthCheckResult:
    """Classify data-directory lock state by real ownership evidence.

    ``owns_lock`` is the caller's SingleInstanceGuard state for THIS
    process: its own expected lock is PASS, not ATTENTION. When the
    current process does not own the lock the actual byte range is
    probed — foreign-held is ATTENTION, free-with-stale-metadata is PASS
    with the leftover owner recorded as crash evidence, never as a live
    holder (#749).
    """

    holder = read_lock_metadata(Path(data_dir))
    if owns_lock:
        return HealthCheckResult(
            check_id='storage.data_dir_lock',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.PASS,
            summary='data directory lock held by this instance (expected)',
            detail=(
                f'owner pid {holder.get("pid")}' if holder else None
            ),
        )
    probe = _probe_lock_state(Path(data_dir) / LOCK_FILENAME)
    if probe == 'held':
        if holder:
            owner = (
                f'pid {holder.get("pid")} on {holder.get("host")}'
            )
        else:
            owner = 'another process'
        status, summary = (
            HealthStatus.ATTENTION,
            f'data directory lock held by {owner}',
        )
        detail = None
    elif probe == 'free':
        if holder:
            status, summary, detail = (
                HealthStatus.PASS,
                'data directory lock is free',
                'stale owner metadata remains from pid '
                f'{holder.get("pid")} (previous session ended without'
                ' cleanup)',
            )
        else:
            status, summary, detail = (
                HealthStatus.PASS,
                'data directory lock is free',
                None,
            )
    else:
        status, summary, detail = (
            HealthStatus.UNKNOWN,
            'data directory lock state could not be probed',
            None,
        )
    return HealthCheckResult(
        check_id='storage.data_dir_lock',
        category=HealthCategory.APP_STORAGE,
        status=status,
        summary=summary,
        detail=detail,
    )


def _check_disk_space(data_dir: Path, min_free_mb: int = 512) -> HealthCheckResult:
    try:
        usage = shutil.disk_usage(Path(data_dir))
    except OSError as exc:
        return HealthCheckResult(
            check_id='storage.disk_space',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.UNKNOWN,
            summary='free disk space unavailable',
            detail=str(exc),
        )
    free_mb = usage.free // (1024 * 1024)
    status = HealthStatus.PASS if free_mb >= min_free_mb else HealthStatus.FAIL
    return HealthCheckResult(
        check_id='storage.disk_space',
        category=HealthCategory.APP_STORAGE,
        status=status,
        summary=f'{free_mb} MiB free (minimum {min_free_mb} MiB)',
    )


def _check_assets_root(data_dir: Path) -> HealthCheckResult:
    root = Path(data_dir) / MANAGED_ASSETS_DIRNAME
    if not root.exists():
        return HealthCheckResult(
            check_id='storage.assets_root',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.NOT_APPLICABLE,
            summary='managed asset root not created yet',
        )
    ok = os.access(root, os.R_OK | os.W_OK)
    return HealthCheckResult(
        check_id='storage.assets_root',
        category=HealthCategory.APP_STORAGE,
        status=HealthStatus.PASS if ok else HealthStatus.FAIL,
        summary='managed asset root accessible' if ok else 'managed asset root not accessible',
        detail=str(root),
    )


def run_health_checks(
    data_dir: Path,
    *,
    integrity_runner: Callable[[Path], HealthCheckResult] | None = None,
    integration_probes: Iterable[Callable[[Path], HealthCheckResult]] = (),
    min_free_mb: int = 512,
    owns_lock: bool = False,
) -> HealthReport:
    """Run bounded, non-mutating health checks.

    ``integrity_runner`` wires the semantic audit (#426,
    ``audit_native_authority_graph``) in without duplicating its validator;
    ``integration_probes`` supplies REW (#599), Capture receiver (#593),
    solver/provider and GPU checks, each classified independently.
    ``owns_lock`` tells the lock check whether THIS process currently owns
    the data-directory lock — pass the live SingleInstanceGuard state so
    diagnostics opened inside a healthy run never flag their own lock.
    """

    results: list[HealthCheckResult] = [
        *_check_database(data_dir),
        _check_lock(data_dir, owns_lock=owns_lock),
        _check_disk_space(data_dir, min_free_mb=min_free_mb),
        _check_assets_root(data_dir),
    ]
    if integrity_runner is not None:
        try:
            results.append(integrity_runner(Path(data_dir)))
        except Exception as exc:  # a crashing probe is a finding, not a crash
            results.append(
                HealthCheckResult(
                    check_id='integrity.semantic',
                    category=HealthCategory.SEMANTIC_INTEGRITY,
                    status=HealthStatus.FAIL,
                    summary='semantic integrity check crashed',
                    detail=f'{type(exc).__name__}: {exc}',
                )
            )
    else:
        results.append(
            HealthCheckResult(
                check_id='integrity.semantic',
                category=HealthCategory.SEMANTIC_INTEGRITY,
                status=HealthStatus.NOT_APPLICABLE,
                summary='semantic integrity audit not wired',
            )
        )
    for index, probe in enumerate(integration_probes):
        try:
            result = probe(Path(data_dir))
        except Exception as exc:
            result = HealthCheckResult(
                check_id=f'integrations.probe_{index}',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.FAIL,
                summary='integration probe raised',
                detail=f'{type(exc).__name__}: {exc}',
            )
        if result.category != HealthCategory.INTEGRATIONS:
            result = result.model_copy(
                update={'category': HealthCategory.INTEGRATIONS}
            )
        results.append(result)
    return HealthReport(checked_at=_utc_now(), results=tuple(results))


# ---------------------------------------------------------------------------
# Previous-session evidence


@dataclass(frozen=True, slots=True)
class UnexpectedEndEvidence:
    unexpected_end: bool
    detail: str


def previous_session_unexpected_end(
    data_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> UnexpectedEndEvidence:
    """Evidence-based detection of an unclean previous session.

    A leftover ``runtime.json`` whose owning pid is dead is positive
    evidence; an absent marker, a live owner, or merely a truncated log is
    not — nothing is inferred from a log simply ending.
    """

    info = read_runtime_info(Path(data_dir))
    if info is None:
        return UnexpectedEndEvidence(False, 'no runtime marker from a previous session')
    if pid_alive is None:
        pid_alive = _default_pid_alive
    try:
        alive = pid_alive(info.pid)
    except Exception:
        alive = False
    if alive:
        return UnexpectedEndEvidence(
            False, f'previous runtime pid {info.pid} still running'
        )
    return UnexpectedEndEvidence(
        True, f'previous runtime pid {info.pid} exited without cleanup'
    )


def _default_pid_alive(pid: int) -> bool:
    if os.name == 'nt':
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Diagnostic package


class PackageCategory(StrEnum):
    LOGS = 'logs'
    BUILD_IDENTITY = 'build_identity'
    SCHEMA_SUMMARY = 'schema_summary'
    HEALTH_RESULTS = 'health_results'
    OPERATION_FAILURES = 'operation_failures'
    CAPABILITY_INVENTORY = 'capability_inventory'
    PREFERENCES_SUMMARY = 'preferences_summary'
    PROJECT_IDS = 'project_ids'


#: Material that must never enter a diagnostic package. The builder only
#: reads the diagnostics directory plus generated manifests — it never walks
#: project databases, measurement assets or capture data.
PACKAGE_EXCLUSIONS: tuple[str, ...] = (
    'raw measurement WAV/REW assets',
    'capture images/depth/mesh',
    'room geometry',
    'serial numbers',
    'user notes',
    'project bundles',
    'credentials/tokens/private pairing keys',
)


class PackagePlan(BaseModel):
    """Privacy preview: the categories a package will contain."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = SUPPORT_SCHEMA_VERSION
    categories: tuple[PackageCategory, ...]
    include_project_ids: bool = False
    byte_budget: int = PACKAGE_BYTE_BUDGET
    exclusions: tuple[str, ...] = PACKAGE_EXCLUSIONS


@dataclass(frozen=True, slots=True)
class PackageResult:
    path: Path
    bytes_written: int
    included: tuple[str, ...]
    skipped: tuple[str, ...]
    manifest_name: str


class DiagnosticPackageBuilder:
    """Builds one bounded local diagnostic archive.

    The archive always contains a ``manifest.json`` describing included
    categories, exclusions and truncation decisions — it is explicitly *not*
    a backup or a project export.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        health_report: HealthReport | None = None,
        operation_failures: Iterable[Mapping[str, Any]] = (),
        capability_inventory: Mapping[str, Any] | None = None,
        preferences_summary: Mapping[str, Any] | None = None,
        project_ids: Mapping[str, Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.health_report = health_report
        self.operation_failures = tuple(operation_failures)
        self.capability_inventory = dict(capability_inventory or {})
        self.preferences_summary = dict(preferences_summary or {})
        self.project_ids = dict(project_ids or {})

    def plan(self, *, include_project_ids: bool = False) -> PackagePlan:
        categories = [PackageCategory.LOGS, PackageCategory.BUILD_IDENTITY]
        categories.append(PackageCategory.SCHEMA_SUMMARY)
        if self.health_report is not None:
            categories.append(PackageCategory.HEALTH_RESULTS)
        if self.operation_failures:
            categories.append(PackageCategory.OPERATION_FAILURES)
        if self.capability_inventory:
            categories.append(PackageCategory.CAPABILITY_INVENTORY)
        categories.append(PackageCategory.PREFERENCES_SUMMARY)
        if include_project_ids and self.project_ids:
            categories.append(PackageCategory.PROJECT_IDS)
        return PackagePlan(
            categories=tuple(categories), include_project_ids=include_project_ids
        )

    def build(self, destination: Path, plan: PackagePlan) -> PackageResult:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        diag_dir = diagnostics_dir(self.data_dir)
        included: list[str] = []
        skipped: list[str] = []
        written = 0

        # Per-member outcomes, recorded verbatim in manifest.json (#749):
        # byte truncation is only ever applied to plain-text logs (tail
        # kept, newest events retained); structured JSON is reduced
        # semantically or skipped whole, never byte-cut.
        members: dict[str, dict[str, Any]] = {}

        def _fits(data: bytes) -> bool:
            return len(data) <= plan.byte_budget - written

        def _write_log(name: str, data: bytes) -> None:
            nonlocal written
            remaining = plan.byte_budget - written
            if remaining <= 0:
                members[name] = {
                    'status': 'skipped',
                    'original_bytes': len(data),
                    'included_bytes': 0,
                }
                skipped.append(name)
                return
            if len(data) > remaining:
                kept = data[-remaining:]
                archive.writestr(name, kept)
                written += len(kept)
                members[name] = {
                    'status': 'truncated',
                    'original_bytes': len(data),
                    'included_bytes': len(kept),
                }
            else:
                archive.writestr(name, data)
                written += len(data)
                members[name] = {
                    'status': 'complete',
                    'original_bytes': len(data),
                    'included_bytes': len(data),
                }
            included.append(name)

        def _write_json(name: str, payload: Any) -> None:
            """Write structured JSON or skip it — never byte-cut (#749).

            List payloads degrade semantically (most recent entries kept,
            still valid JSON); everything else is skipped whole when it
            does not fit the remaining payload budget.
            """
            nonlocal written
            data = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, default=str
            ).encode('utf-8')
            if _fits(data):
                archive.writestr(name, data)
                written += len(data)
                members[name] = {
                    'status': 'complete',
                    'original_bytes': len(data),
                    'included_bytes': len(data),
                }
                included.append(name)
                return
            if isinstance(payload, list):
                # binary-search the largest tail that still fits
                lo, hi = 0, len(payload)
                best: bytes | None = None
                best_records = -1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    candidate = json.dumps(
                        payload[len(payload) - mid :] if mid else [],
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode('utf-8')
                    if _fits(candidate):
                        best, best_records = candidate, mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best is not None and best_records > 0:
                    archive.writestr(name, best)
                    written += len(best)
                    members[name] = {
                        'status': 'truncated',
                        'original_bytes': len(data),
                        'included_bytes': len(best),
                        'original_records': len(payload),
                        'included_records': best_records,
                    }
                    included.append(name)
                    return
            members[name] = {
                'status': 'skipped',
                'original_bytes': len(data),
                'included_bytes': 0,
            }
            skipped.append(name)

        with zipfile.ZipFile(
            destination, 'w', compression=zipfile.ZIP_DEFLATED
        ) as archive:
            if PackageCategory.LOGS in plan.categories and diag_dir.exists():
                for log_file in sorted(diag_dir.glob(f'{LOG_FILENAME}*')):
                    try:
                        _write_log(
                            f'logs/{log_file.name}', log_file.read_bytes()
                        )
                    except OSError:
                        members[f'logs/{log_file.name}'] = {
                            'status': 'skipped',
                            'reason': 'unreadable',
                        }
                        skipped.append(f'logs/{log_file.name}')

            identity = build_identity()
            if PackageCategory.BUILD_IDENTITY in plan.categories:
                _write_json(
                    'build_identity.json',
                    {
                        'version': identity.version,
                        'python': identity.python,
                        'platform': identity.platform,
                        'frozen': identity.frozen,
                        'qt': identity.qt,
                    },
                )
            if PackageCategory.SCHEMA_SUMMARY in plan.categories:
                _write_json(
                    'schema_summary.json',
                    {'schema_version': _schema_version(), 'app_version': __version__},
                )
            if PackageCategory.HEALTH_RESULTS in plan.categories and self.health_report:
                _write_json(
                    'health_checks.json',
                    self.health_report.model_dump(mode='json'),
                )
            if PackageCategory.OPERATION_FAILURES in plan.categories:
                _write_json(
                    'operation_failures.json',
                    list(self.operation_failures),
                )
            if PackageCategory.CAPABILITY_INVENTORY in plan.categories:
                _write_json('capability_inventory.json', self.capability_inventory)
            if PackageCategory.PREFERENCES_SUMMARY in plan.categories:
                _write_json('preferences_summary.json', self._sanitized_preferences())
            if plan.include_project_ids and self.project_ids:
                _write_json('project_ids.json', self.project_ids)

            manifest = {
                'package': PACKAGE_PREFIX,
                'schema_version': SUPPORT_SCHEMA_VERSION,
                'created_at': _utc_now(),
                'categories': [c.value for c in plan.categories],
                'exclusions': list(plan.exclusions),
                'not_a_backup': True,
                'not_a_project_export': True,
                'included_files': included,
                'skipped_files': skipped,
                'members': members,
                'bytes': written,
                'byte_budget': plan.byte_budget,
                # The budget bounds member payload bytes, not the final
                # archive: ZIP container overhead and manifest.json sit
                # outside it. ``archive_bytes`` (filled post-close) is the
                # authoritative produced size (#749).
                'budget_scope': 'member_payload_bytes',
            }
            archive.writestr(
                'manifest.json', json.dumps(manifest, indent=2)
            )

        archive_bytes = destination.stat().st_size
        return PackageResult(
            path=destination,
            bytes_written=archive_bytes,
            included=tuple(included),
            skipped=tuple(skipped),
            manifest_name='manifest.json',
        )

    def _sanitized_preferences(self) -> dict[str, Any]:
        """Preferences summary without path values or credential-shaped keys."""

        safe: dict[str, Any] = {}
        for key, value in self.preferences_summary.items():
            lowered = key.lower()
            if any(
                token in lowered
                for token in ('key', 'token', 'secret', 'password', 'credential')
            ):
                safe[key] = '<excluded>'
            elif self._looks_like_path(value):
                safe[key] = '<set>' if value else '<unset>'
            else:
                safe[key] = value
        return safe

    @staticmethod
    def _looks_like_path(value: Any) -> bool:
        return isinstance(value, str) and (
            os.sep in value or '/' in value or '\\' in value
        )


def package_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    return f'{PACKAGE_PREFIX}-{stamp}.zip'


__all__ = [
    'DATABASE_NAME',
    'DiagnosticPackageBuilder',
    'EnvironmentSummary',
    'HealthCategory',
    'HealthCheckResult',
    'HealthReport',
    'HealthStatus',
    'PACKAGE_BYTE_BUDGET',
    'PACKAGE_EXCLUSIONS',
    'PACKAGE_PREFIX',
    'PackageCategory',
    'PackagePlan',
    'PackageResult',
    'SUPPORT_SCHEMA_VERSION',
    'UnexpectedEndEvidence',
    'environment_summary',
    'failure_correlation_id',
    'package_filename',
    'previous_session_unexpected_end',
    'run_health_checks',
]
