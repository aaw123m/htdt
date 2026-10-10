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
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .ingress import IngressTooLargeError, read_file_bounded
from .managed_assets import MANAGED_ASSETS_DIRNAME
from .native_diagnostics import (
    BuildIdentity,
    DIAGNOSTICS_DIRNAME,
    LOG_FILENAME,
    MAX_LOG_BYTES,
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


#: Archive manifest schema. v2 adds per-member sha256/classification/
#: redactions, ``excluded_categories``/``collection_errors`` (#884).
SUPPORT_SCHEMA_VERSION = 2
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


@contextmanager
def _staged_zip_archive(destination: Path):
    """Open a ZIP for writing on a sibling temp file; promote on success.

    A failure anywhere in the block removes the staged file — the chosen
    path never holds a truncated archive.
    """

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f'.{destination.name}.',
        suffix='.tmp',
        dir=destination.parent,
    )
    os.close(descriptor)
    staging = Path(temp_name)
    try:
        with zipfile.ZipFile(
            staging, 'w', compression=zipfile.ZIP_DEFLATED
        ) as archive:
            yield archive
        os.replace(staging, destination)
    except BaseException:  # error-boundary: cleanup before re-raise — any failure (incl. cancel) removes the staged archive so a partial file never looks published (noqa: BLE001)
        staging.unlink(missing_ok=True)
        raise


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
    """Native store schema version this build supports.

    The diagnostic surface covers ``cad-scenes.sqlite3`` — the native store
    governed by ``cad_schema``'s ``native_schema_metadata`` authority — so
    the reported version must come from that lineage, not the legacy
    server store's ``database.SCHEMA_VERSION``.
    """

    from .cad_schema import NATIVE_SCHEMA_VERSION  # local import keeps module Qt/db-light

    return NATIVE_SCHEMA_VERSION


def _store_schema_summary(data_dir: Path) -> dict[str, Any]:
    """Stored-vs-supported schema state for the native store.

    Support needs the real store authority, not just the app constant:
    a store written by a newer build, pending migration, or unreadable
    schema metadata is the difference between "update the app" and
    "restore a backup".
    """

    from .cad_schema import (  # local import keeps module Qt/db-light
        NATIVE_SCHEMA_VERSION,
        NativeSchemaError,
        native_schema_compatibility,
        read_native_schema_version,
    )

    summary: dict[str, Any] = {
        'supported_native_schema_version': NATIVE_SCHEMA_VERSION,
    }
    database_path = Path(data_dir) / DATABASE_NAME
    if not database_path.is_file() or database_path.stat().st_size == 0:
        summary['stored_native_schema_version'] = None
        summary['native_schema_compatibility'] = 'no_store'
        return summary
    try:
        stored = read_native_schema_version(database_path)
    except (NativeSchemaError, sqlite3.Error) as exc:
        summary['stored_native_schema_version'] = None
        summary['native_schema_compatibility'] = 'unreadable'
        summary['schema_error'] = str(exc)
        return summary
    summary['stored_native_schema_version'] = stored
    summary['native_schema_compatibility'] = native_schema_compatibility(stored)
    return summary


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
            absent.model_copy(
                update={'check_id': 'storage.schema_compatibility'}
            ),
        ]
    try:
        # ``with sqlite3.connect`` alone commits/rolls back but never
        # closes — wrap in closing() so the health probe cannot pin an
        # open descriptor on the project database on the error path too.
        with closing(
            sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
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
            HealthCheckResult(
                check_id='storage.schema_compatibility',
                category=HealthCategory.APP_STORAGE,
                status=HealthStatus.FAIL,
                summary='schema compatibility could not be evaluated',
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
    results.append(_check_schema_compatibility(path))
    return results


def _check_schema_compatibility(path: Path) -> HealthCheckResult:
    """Native-store schema verdict from the app's own schema authority.

    ``check_native_schema_compatibility`` is the same read-path gate every
    repository pays: a store it rejects (newer schema, alien tables,
    unreadable metadata) is a real fault even though the file opens and
    its pages are structurally sound — "opens + integrity ok" was
    reporting healthy on a store the app refuses to load.
    """

    from .cad_schema import (  # local import keeps module Qt/db-light
        NATIVE_SCHEMA_VERSION,
        NativeSchemaError,
        check_native_schema_compatibility,
    )

    try:
        stored_version = check_native_schema_compatibility(path)
    except NativeSchemaError as exc:
        return HealthCheckResult(
            check_id='storage.schema_compatibility',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.FAIL,
            summary='project database schema is not supported by this build',
            detail=(
                f'{exc}. Restore a backup from before the data was written '
                'or install the HTDT build that created it.'
            ),
        )
    except Exception as exc:  # error-boundary: health check — a crashing check is itself a FAIL finding with the exception identity, never a blanked report (noqa: BLE001)
        return HealthCheckResult(
            check_id='storage.schema_compatibility',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.FAIL,
            summary='schema compatibility could not be evaluated',
            detail=f'{type(exc).__name__}: {exc}',
        )
    if stored_version == NATIVE_SCHEMA_VERSION:
        return HealthCheckResult(
            check_id='storage.schema_compatibility',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.PASS,
            summary=f'project database schema v{stored_version} is current',
        )
    if stored_version >= 1:
        return HealthCheckResult(
            check_id='storage.schema_compatibility',
            category=HealthCategory.APP_STORAGE,
            status=HealthStatus.ATTENTION,
            summary=(
                f'project database schema v{stored_version} predates this build'
            ),
            detail=(
                f'supported schema is v{NATIVE_SCHEMA_VERSION}; the store '
                'migrates on next open (a recovery copy is taken first)'
            ),
        )
    return HealthCheckResult(
        check_id='storage.schema_compatibility',
        category=HealthCategory.APP_STORAGE,
        status=HealthStatus.ATTENTION,
        summary='project database is empty or pre-versioning',
        detail='the store is adopted or initialized on next open',
    )


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
    """Free space on the volume holding the data directory.

    A not-yet-created data directory (first launch, fresh profile) still
    has a real answer: measure the nearest existing ancestor — the volume
    is the same — instead of reporting the disk as unmeasurable.
    """

    probe = Path(data_dir)
    probed = probe
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
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
        detail=(
            f'measured on {probe}' if probe != probed else None
        ),
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

    def storage_check(
        check_id: str,
        produce: Callable[[], Iterable[HealthCheckResult]],
    ) -> list[HealthCheckResult]:
        # Same isolation contract as the probes below: a check that crashes
        # is itself a finding, never a blanked report.
        try:
            return list(produce())
        except Exception as exc:  # error-boundary: health check — a crashing check is itself a FAIL finding with the exception identity, never a blanked report (noqa: BLE001)
            return [
                HealthCheckResult(
                    check_id=check_id,
                    category=HealthCategory.APP_STORAGE,
                    status=HealthStatus.FAIL,
                    summary='storage check crashed',
                    detail=f'{type(exc).__name__}: {exc}',
                )
            ]

    results: list[HealthCheckResult] = [
        *storage_check(
            'storage.database', lambda: _check_database(data_dir)
        ),
        *storage_check(
            'storage.data_dir_lock',
            lambda: [_check_lock(data_dir, owns_lock=owns_lock)],
        ),
        *storage_check(
            'storage.disk_space',
            lambda: [_check_disk_space(data_dir, min_free_mb=min_free_mb)],
        ),
        *storage_check(
            'storage.assets_root',
            lambda: [_check_assets_root(data_dir)],
        ),
    ]
    if integrity_runner is not None:
        try:
            results.append(integrity_runner(Path(data_dir)))
        except Exception as exc:  # error-boundary: health check — a crashing probe is itself a FAIL finding with the exception identity, never a crash (noqa: BLE001)
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
        except Exception as exc:  # error-boundary: health check — a crashing probe is itself a FAIL finding with the exception identity, never a crash (noqa: BLE001)
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
# Live-store check runners and probes (#1018)
#
# These factories produce the ``integrity_runner``/``integration_probes``
# callables :func:`run_health_checks` consumes. Every one is read-only on
# the project store: the semantic audit runs on a consistent throwaway
# clone (repository construction may initialize/migrate a schema, so the
# live file is never opened), and the integration probes only *read*
# endpoint/snapshot state — none of them connects a device, promotes
# evidence, or repairs anything.


def semantic_integrity_check(
    data_dir: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> HealthCheckResult:
    """The #426 authority-graph audit as a ``SEMANTIC_INTEGRITY`` finding.

    The audit replays persisted authority through repository construction,
    which may initialize or migrate the schema — running it on the live
    database would itself be a write. The probe therefore mirrors the
    ``native_upgrade`` contract: a consistent online-backup clone plus a
    hardlinked managed-asset subtree inside one temporary directory under
    ``data_dir`` (the audit resolves ``measurement-assets/`` relative to
    the audited file's parent), removed in ``finally`` whatever happens.
    The live store is only read, through SQLite's backup API so a torn
    mid-transaction copy can never masquerade as corruption evidence.
    """

    data_dir = Path(data_dir)
    database_path = data_dir / DATABASE_NAME
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return HealthCheckResult(
            check_id='integrity.semantic',
            category=HealthCategory.SEMANTIC_INTEGRITY,
            status=HealthStatus.NOT_APPLICABLE,
            summary='project database not created yet',
        )
    from .cad_schema import connect_sqlite  # local import keeps module Qt/db-light
    from .native_authority_audit import audit_native_authority_graph

    probe_dir = Path(tempfile.mkdtemp(prefix='.health-audit-', dir=data_dir))
    try:
        probe_path = probe_dir / DATABASE_NAME
        with closing(
            sqlite3.connect(
                f'file:{database_path.as_posix()}?mode=ro', uri=True
            )
        ) as source, closing(connect_sqlite(probe_path)) as destination:
            source.backup(destination)
            destination.commit()
        live_assets = data_dir / MANAGED_ASSETS_DIRNAME
        if live_assets.is_dir():
            probe_assets = probe_dir / MANAGED_ASSETS_DIRNAME
            probe_assets.mkdir()
            for candidate in live_assets.iterdir():
                if not candidate.is_file():
                    continue
                target = probe_assets / candidate.name
                try:
                    target.hardlink_to(candidate)
                except OSError:
                    shutil.copyfile(candidate, target)
        report = audit_native_authority_graph(
            probe_path, is_cancelled=is_cancelled
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    if report.ok:
        total = sum(count for _name, count in report.checked)
        return HealthCheckResult(
            check_id='integrity.semantic',
            category=HealthCategory.SEMANTIC_INTEGRITY,
            status=HealthStatus.PASS,
            summary=f'authority graph audit passed ({total} records checked)',
        )
    classes = sorted({d.failure_class for d in report.diagnostics})
    detail_lines = [
        f'{len(report.diagnostics)} diagnostics'
        + (f' ({", ".join(classes)})' if classes else '')
    ]
    for diagnostic in report.diagnostics[:5]:
        detail_lines.append(
            f'[{diagnostic.authority}:{diagnostic.record_ref}] '
            f'{diagnostic.failure_class} — {diagnostic.message}'
        )
    if len(report.diagnostics) > 5:
        detail_lines.append(
            f'... {len(report.diagnostics) - 5} more'
        )
    for table in report.unclassified_tables[:5]:
        detail_lines.append(f'[coverage:{table}] unclassified table')
    return HealthCheckResult(
        check_id='integrity.semantic',
        category=HealthCategory.SEMANTIC_INTEGRITY,
        status=HealthStatus.FAIL,
        summary='authority graph audit reported stale or broken records',
        detail='\n'.join(detail_lines),
    )


def rew_api_probe(
    base_url: str,
    *,
    timeout_s: float = 1.5,
) -> Callable[[Path], HealthCheckResult]:
    """REW API reachability probe (#599 surface, read-only).

    ``base_url`` is captured by the caller on the UI thread; the probe
    itself only constructs a client and performs the bounded ``status``
    read. An unreachable REW is ATTENTION — it is an optional integration,
    never installation damage.
    """

    def probe(_data_dir: Path) -> HealthCheckResult:
        from .rew_api import RewApiClient  # local import keeps module light

        client = RewApiClient(base_url, timeout_s=timeout_s)
        status = client.status()
        if status['connected']:
            version = status['rew_version'] or 'version unknown'
            return HealthCheckResult(
                check_id='integrations.rew_api',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.PASS,
                summary=f'REW API reachable ({version})',
                detail=(
                    f'{status["base_url"]} — '
                    f'{status["measurement_count"]} measurements visible'
                ),
            )
        return HealthCheckResult(
            check_id='integrations.rew_api',
            category=HealthCategory.INTEGRATIONS,
            status=HealthStatus.ATTENTION,
            summary='REW API is not reachable (optional integration)',
            detail=status['error'] or str(status['base_url']),
        )

    return probe


def capture_receiver_probe(
    state: Mapping[str, object],
) -> Callable[[Path], HealthCheckResult]:
    """Capture receiver (#593) probe over a UI-thread state snapshot.

    ``state`` is frozen before dispatch — the receiver's service objects
    are not thread-safe, so the worker must never query them live. Keys:
    ``running``, ``requested_enabled``, ``last_error``, ``enabled``.
    ``enabled=False`` (no receiver wired at all) is honest NOT_APPLICABLE,
    not a failure.
    """

    def probe(_data_dir: Path) -> HealthCheckResult:
        if not state.get('enabled', True):
            return HealthCheckResult(
                check_id='integrations.capture_receiver',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.NOT_APPLICABLE,
                summary='capture receiver is not configured in this build',
            )
        if state.get('running'):
            return HealthCheckResult(
                check_id='integrations.capture_receiver',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.PASS,
                summary='capture receiver is listening',
            )
        if state.get('requested_enabled'):
            return HealthCheckResult(
                check_id='integrations.capture_receiver',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.ATTENTION,
                summary='capture receiver is enabled but not listening',
                detail=(
                    str(state.get('last_error'))
                    if state.get('last_error')
                    else 'receiver is stopped'
                ),
            )
        return HealthCheckResult(
            check_id='integrations.capture_receiver',
            category=HealthCategory.INTEGRATIONS,
            status=HealthStatus.NOT_APPLICABLE,
            summary='capture receiver is disabled',
        )

    return probe


def vtk_probe() -> Callable[[Path], HealthCheckResult]:
    """3D stack presence probe — capability presence, never a GL context.

    Mirrors ``collect_gpu_context``'s rule: forcing a GL context to check
    health can crash exactly the machines whose graphics stack is broken,
    so this only answers whether the VTK module is importable.
    """

    def probe(_data_dir: Path) -> HealthCheckResult:
        import importlib.util

        if importlib.util.find_spec('vtkmodules') is not None:
            return HealthCheckResult(
                check_id='integrations.vtk',
                category=HealthCategory.INTEGRATIONS,
                status=HealthStatus.PASS,
                summary='VTK module is importable (renderer unprobed)',
            )
        return HealthCheckResult(
            check_id='integrations.vtk',
            category=HealthCategory.INTEGRATIONS,
            status=HealthStatus.ATTENTION,
            summary='VTK module is not importable — 3D surfaces unavailable',
            detail='room/preview 3D workspaces require vtkmodules',
        )

    return probe


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
    except Exception:  # error-boundary: environment probe — a pid-liveness probe failure treats the pid as dead honestly (noqa: BLE001)
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
    LAUNCH_METADATA = 'launch_metadata'
    # #884 field-level context collectors
    RUNTIME_CONTEXT = 'runtime_context'
    GPU_CONTEXT = 'gpu_context'
    AUDIO_CONTEXT = 'audio_context'
    ADAPTER_CAPABILITY = 'adapter_capability'
    WORKFLOW_STATE = 'workflow_state'
    RELEASE_EVIDENCE = 'release_evidence'
    MEASUREMENT_ENGINE_STATUS = 'measurement_engine_status'
    DEVICE_TRANSACTION_STATUS = 'device_transaction_status'


class FieldClassification(StrEnum):
    """Per-member privacy classification (#884).

    The archive is only ever populated from these classes — a field or
    member the collector cannot prove safe is simply never produced;
    ``SENSITIVE_PROJECT_DATA`` is attached only by explicit operator
    opt-in, and ``SECRET`` marks material that must never be collected
    at all (declared for the ledger, structurally unwriteable).
    """

    SAFE_DIAGNOSTIC = 'SAFE_DIAGNOSTIC'
    PROJECT_METADATA = 'PROJECT_METADATA'
    SENSITIVE_PROJECT_DATA = 'SENSITIVE_PROJECT_DATA'
    SECRET = 'SECRET_NEVER_EXPORT'


#: Classification per category. Anything not listed here is defaulted to
#: SAFE_DIAGNOSTIC at write time; categories absent from the map are an
#: authoring error caught by tests — the write path never guesses.
CATEGORY_CLASSIFICATION: dict[PackageCategory, FieldClassification] = {
    PackageCategory.LOGS: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.BUILD_IDENTITY: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.SCHEMA_SUMMARY: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.HEALTH_RESULTS: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.OPERATION_FAILURES: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.CAPABILITY_INVENTORY: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.PREFERENCES_SUMMARY: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.LAUNCH_METADATA: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.PROJECT_IDS: FieldClassification.PROJECT_METADATA,
    PackageCategory.RUNTIME_CONTEXT: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.GPU_CONTEXT: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.AUDIO_CONTEXT: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.ADAPTER_CAPABILITY: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.WORKFLOW_STATE: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.RELEASE_EVIDENCE: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.MEASUREMENT_ENGINE_STATUS: FieldClassification.SAFE_DIAGNOSTIC,
    PackageCategory.DEVICE_TRANSACTION_STATUS: FieldClassification.SAFE_DIAGNOSTIC,
}


# ---------------------------------------------------------------------------
# #884 write-time redaction
#
# Redaction runs on EVERY member payload at write time — a collector bug or
# a future category can never smuggle a path/host/secret past it. Counts are
# recorded per member in the manifest so the operator can see exactly what
# was scrubbed.

_BUNDLE_SECRET_KEY = re.compile(
    r'\b('
    r'passwords?|passwds?|secrets?|tokens?|api[-_]?keys?|apikeys?|'
    r'authorizations?|credentials?|private[-_]?keys?'
    r')\b(\s*["\']?\s*[=:]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;}\'"]+)',
    re.IGNORECASE,
)
_BUNDLE_WINDOWS_PATH = re.compile(
    r'\b[A-Za-z]:[\\/](?:[^\s"\'<>|]+[\\/])*[^\s"\'<>|]*')
_BUNDLE_UNC_PATH = re.compile(r'\\\\[^\s"\'<>|]+')
_BUNDLE_POSIX_PATH = re.compile(
    r'(?<![\w.-])(?:/(?:home|Users|usr|opt|var|tmp|private|etc)'
    r'(?:/[^\s"\'<>|]+)+)')
_BUNDLE_IPV4 = re.compile(
    r'\b(?:25[0-5]|2[0-4]\d|1?\d?\d)'
    r'(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b')
# IPv6 requires ≥3 groups AND at least one a-f letter so HH:MM:SS
# timestamps and plain numeric runs are never redacted.
_BUNDLE_IPV6 = re.compile(
    r'\b(?=[0-9a-fA-F:]{4,}[a-fA-F])(?:[0-9a-fA-F]{0,4}:){2,7}'
    r'[0-9a-fA-F]{0,4}\b')
_BUNDLE_TOKEN_VALUE = re.compile(
    r'\b(?:[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|'  # JWT
    r'(?:sk|pk|ghp|gho|xox[bpsar]|Bearer)[-_][A-Za-z0-9]{16,}|'
    r'\b[0-9a-fA-F]{64})\b')
_BUNDLE_SENSITIVE_KEY = re.compile(
    r'host(name)?|computer[-_]?name|user[-_]?name|owner|operator[-_]?name|'
    r'customer|person|serial[-_]?number|address|location',
    re.IGNORECASE,
)
# Structured-value keys that are secret by NAME — the value is replaced
# wholesale regardless of its shape (dict branch of redact_support_value).
_BUNDLE_SECRET_FIELD_KEY = re.compile(
    r'password|passwd|secret|token|api[-_]?key|credential|'
    r'private[-_]?key|authorization|cert(ificate)?[-_]?(key|pem|material)',
    re.IGNORECASE,
)

_REDACTED = '<redacted>'


def _redact_text(text: str, counts: dict[str, int]) -> str:
    def _bump(name: str, repl: str):
        def _sub(match: re.Match) -> str:
            counts[name] = counts.get(name, 0) + 1
            return repl
        return _sub

    text = _BUNDLE_SECRET_KEY.sub(
        lambda m: counts.__setitem__('secrets', counts.get('secrets', 0) + 1)
        or f'{m.group(1)}{m.group(2)}{_REDACTED}', text)
    text = _BUNDLE_UNC_PATH.sub(_bump('paths', '<unc-path>'), text)
    text = _BUNDLE_WINDOWS_PATH.sub(_bump('paths', '<path>'), text)
    text = _BUNDLE_POSIX_PATH.sub(_bump('paths', '<path>'), text)
    text = _BUNDLE_IPV4.sub(_bump('hosts', '<ip>'), text)
    text = _BUNDLE_IPV6.sub(_bump('hosts', '<ip>'), text)
    text = _BUNDLE_TOKEN_VALUE.sub(_bump('tokens', _REDACTED), text)
    return text


def redact_support_value(
    value: Any,
    counts: dict[str, int] | None = None,
    *,
    _inside_sensitive_key: bool = False,
) -> Any:
    """Recursively redact a JSON-shaped value.

    Returns the redacted value; ``counts`` accumulates per-kind counters
    (``paths``/``hosts``/``secrets``/``tokens``/``keys``) for the manifest.
    A dict key that looks like a host/user/customer/serial field has its
    *value* replaced — the key itself stays so the shape remains readable.
    """
    if counts is None:
        counts = {}
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _BUNDLE_SECRET_FIELD_KEY.search(key_text):
                # Secret by name — replace the value wholesale.
                counts['secrets'] = counts.get('secrets', 0) + 1
                redacted[key] = _REDACTED
            elif _BUNDLE_SENSITIVE_KEY.search(key_text):
                counts['keys'] = counts.get('keys', 0) + 1
                redacted[key] = (
                    _REDACTED if item not in (None, '', [], {})
                    else item
                )
            else:
                redacted[key] = redact_support_value(
                    item, counts)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_support_value(item, counts) for item in value]
    if isinstance(value, str):
        return _redact_text(value, counts)
    return value


class BundleMemberPreview(BaseModel):
    """One member exactly as it will be written to the archive."""

    model_config = ConfigDict(frozen=True)

    name: str
    classification: FieldClassification
    status: Literal['complete', 'truncated', 'skipped']
    included_bytes: int = Field(ge=0)
    sha256: str | None = None
    """sha256 of the member bytes as written (None when skipped)."""
    redactions: dict[str, int] = Field(default_factory=dict)
    detail: str = ''


class BundlePreview(BaseModel):
    """The exact export content before anything is written (#884).

    Produced by the same staging pass as :meth:`DiagnosticPackageBuilder.build`
    — the preview can never disagree with the exported archive.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int
    members: tuple[BundleMemberPreview, ...]
    categories: tuple[PackageCategory, ...]
    excluded_categories: tuple[PackageCategory, ...]
    exclusions: tuple[str, ...]
    collection_errors: tuple[str, ...]
    byte_budget: int
    included_bytes: int = Field(ge=0)
    preview_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    """Canonical sha over the staged member set — identical state produces
    an identical preview hash (archive determinism is a separate concern)."""


#: #884 categories populated by ``context_providers`` — pluggable so the
#: builder stays Qt/hardware-light and tests can inject synthetic payloads.
_CONTEXT_CATEGORIES: tuple[PackageCategory, ...] = (
    PackageCategory.RUNTIME_CONTEXT,
    PackageCategory.GPU_CONTEXT,
    PackageCategory.AUDIO_CONTEXT,
    PackageCategory.ADAPTER_CAPABILITY,
    PackageCategory.WORKFLOW_STATE,
    PackageCategory.RELEASE_EVIDENCE,
    PackageCategory.MEASUREMENT_ENGINE_STATUS,
    PackageCategory.DEVICE_TRANSACTION_STATUS,
)


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


@dataclass(slots=True)
class _StagedPackage:
    """Members after collection + redaction + budget, before zipping."""

    plan: PackagePlan
    members: dict[str, bytes] = field(default_factory=dict)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    included: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)
    written: int = 0


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
        context_providers: Mapping[PackageCategory, Callable[[], Any]] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.health_report = health_report
        self.operation_failures = tuple(operation_failures)
        self.capability_inventory = dict(capability_inventory or {})
        self.preferences_summary = dict(preferences_summary or {})
        self.project_ids = dict(project_ids or {})
        # #884: lazy collectors keyed by category. A provider that raises is
        # recorded as a collection error in the manifest — the export must
        # never fail because one probe crashed.
        self.context_providers = dict(context_providers or {})

    def plan(self, *, include_project_ids: bool = False) -> PackagePlan:
        categories = [PackageCategory.LOGS, PackageCategory.BUILD_IDENTITY]
        categories.append(PackageCategory.SCHEMA_SUMMARY)
        if (
            diagnostics_dir(self.data_dir) / 'recovery-launch-metadata.json'
        ).is_file():
            categories.append(PackageCategory.LAUNCH_METADATA)
        if self.health_report is not None:
            categories.append(PackageCategory.HEALTH_RESULTS)
        if self.operation_failures:
            categories.append(PackageCategory.OPERATION_FAILURES)
        if self.capability_inventory:
            categories.append(PackageCategory.CAPABILITY_INVENTORY)
        categories.append(PackageCategory.PREFERENCES_SUMMARY)
        if include_project_ids and self.project_ids:
            categories.append(PackageCategory.PROJECT_IDS)
        for category in _CONTEXT_CATEGORIES:
            if category in self.context_providers:
                categories.append(category)
        return PackagePlan(
            categories=tuple(categories), include_project_ids=include_project_ids
        )

    def _stage(self, plan: PackagePlan) -> '_StagedPackage':
        """Collect + redact + bound every member without writing an archive.

        Shared by :meth:`preview` and :meth:`build` so the preview is
        byte-identical to the export (#884).
        """
        staged = _StagedPackage(plan=plan)
        diag_dir = diagnostics_dir(self.data_dir)
        included: list[str] = staged.included
        skipped: list[str] = staged.skipped
        members: dict[str, dict[str, Any]] = staged.meta
        written = 0

        def _fits(data: bytes) -> bool:
            return len(data) <= plan.byte_budget - written

        def _mark(
            name: str,
            data: bytes,
            meta: dict[str, Any],
            category: PackageCategory,
            counts: dict[str, int],
        ) -> None:
            nonlocal written
            staged.members[name] = data
            written += len(data)
            meta.update(
                {
                    'original_bytes': meta.get('original_bytes', len(data)),
                    'included_bytes': len(data),
                    'sha256': sha256(data).hexdigest(),
                    'classification': CATEGORY_CLASSIFICATION[category].value,
                    'redactions': dict(counts),
                }
            )
            members[name] = meta
            included.append(name)

        def _write_log(
            name: str, data: bytes, category: PackageCategory = PackageCategory.LOGS
        ) -> None:
            counts: dict[str, int] = {}
            original = len(data)
            data = _redact_text(
                data.decode('utf-8', errors='replace'), counts
            ).encode('utf-8')
            remaining = plan.byte_budget - written
            if remaining <= 0:
                members[name] = {
                    'status': 'skipped',
                    'original_bytes': original,
                    'included_bytes': 0,
                    'classification': CATEGORY_CLASSIFICATION[category].value,
                    'redactions': dict(counts),
                }
                skipped.append(name)
                return
            if len(data) > remaining:
                kept = data[-remaining:]
                _mark(
                    name, kept,
                    {'status': 'truncated', 'original_bytes': original},
                    category, counts,
                )
            else:
                _mark(
                    name, data,
                    {'status': 'complete', 'original_bytes': original},
                    category, counts,
                )

        def _write_json(
            name: str,
            payload: Any,
            category: PackageCategory,
        ) -> None:
            """Write structured JSON or skip it — never byte-cut (#749).

            Payload is redacted BEFORE serialization; list payloads degrade
            semantically (most recent entries kept, still valid JSON);
            everything else is skipped whole when it does not fit the
            remaining payload budget.
            """
            counts: dict[str, int] = {}
            payload = redact_support_value(payload, counts)
            data = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, default=str
            ).encode('utf-8')
            if _fits(data):
                _mark(name, data, {'status': 'complete'}, category, counts)
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
                    _mark(
                        name, best,
                        {
                            'status': 'truncated',
                            'original_bytes': len(data),
                            'original_records': len(payload),
                            'included_records': best_records,
                        },
                        category, counts,
                    )
                    return
            members[name] = {
                'status': 'skipped',
                'original_bytes': len(data),
                'included_bytes': 0,
                'classification': CATEGORY_CLASSIFICATION[category].value,
                'redactions': dict(counts),
            }
            skipped.append(name)

        if PackageCategory.LOGS in plan.categories and diag_dir.exists():
            for log_file in sorted(diag_dir.glob(f'{LOG_FILENAME}*')):
                try:
                    _write_log(
                        f'logs/{log_file.name}',
                        read_file_bounded(
                            log_file, 8 * MAX_LOG_BYTES, label='log file'
                        ),
                    )
                except IngressTooLargeError:
                    members[f'logs/{log_file.name}'] = {
                        'status': 'skipped',
                        'reason': 'too large',
                        'classification': FieldClassification.SAFE_DIAGNOSTIC.value,
                    }
                    skipped.append(f'logs/{log_file.name}')
                except OSError:
                    members[f'logs/{log_file.name}'] = {
                        'status': 'skipped',
                        'reason': 'unreadable',
                        'classification': FieldClassification.SAFE_DIAGNOSTIC.value,
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
                PackageCategory.BUILD_IDENTITY,
            )
        if PackageCategory.SCHEMA_SUMMARY in plan.categories:
            _write_json(
                'schema_summary.json',
                {
                    'schema_version': _schema_version(),
                    'app_version': __version__,
                    **_store_schema_summary(self.data_dir),
                },
                PackageCategory.SCHEMA_SUMMARY,
            )
        if PackageCategory.LAUNCH_METADATA in plan.categories:
            self._write_launch_metadata(
                plan,
                lambda name, payload: _write_json(
                    name, payload, PackageCategory.LAUNCH_METADATA
                ),
                members,
                skipped,
            )
        if (
            PackageCategory.HEALTH_RESULTS in plan.categories
            and self.health_report
        ):
            _write_json(
                'health_checks.json',
                self.health_report.model_dump(mode='json'),
                PackageCategory.HEALTH_RESULTS,
            )
        if PackageCategory.OPERATION_FAILURES in plan.categories:
            _write_json(
                'operation_failures.json',
                list(self.operation_failures),
                PackageCategory.OPERATION_FAILURES,
            )
        if PackageCategory.CAPABILITY_INVENTORY in plan.categories:
            _write_json(
                'capability_inventory.json',
                self.capability_inventory,
                PackageCategory.CAPABILITY_INVENTORY,
            )
        if PackageCategory.PREFERENCES_SUMMARY in plan.categories:
            _write_json(
                'preferences_summary.json',
                self._sanitized_preferences(),
                PackageCategory.PREFERENCES_SUMMARY,
            )
        if plan.include_project_ids and self.project_ids:
            _write_json(
                'project_ids.json',
                self.project_ids,
                PackageCategory.PROJECT_IDS,
            )

        # #884 context collectors — each wrapped so a crashed probe is a
        # recorded collection error, never a failed export.
        for category in _CONTEXT_CATEGORIES:
            if category not in plan.categories:
                continue
            provider = self.context_providers.get(category)
            if provider is None:
                continue
            try:
                payload = provider()
            except Exception as exc:  # error-boundary: probe isolation
                staged.collection_errors.append(
                    f'{category.value}: {type(exc).__name__}'
                )
                continue
            if payload is None:
                continue
            _write_json(f'context/{category.value}.json', payload, category)

        staged.written = written
        return staged

    def preview(self, plan: PackagePlan) -> BundlePreview:
        """The exact export content before anything is written (#884)."""
        staged = self._stage(plan)
        member_previews = tuple(
            BundleMemberPreview(
                name=name,
                classification=FieldClassification(
                    meta.get(
                        'classification',
                        FieldClassification.SAFE_DIAGNOSTIC.value,
                    )
                ),
                status=meta['status'],
                included_bytes=meta.get('included_bytes', 0),
                sha256=meta.get('sha256'),
                redactions=meta.get('redactions', {}),
                detail=str(meta.get('reason', '')),
            )
            for name, meta in staged.meta.items()
        )
        digest = sha256(
            '\n'.join(
                f'{name}:{staged.meta[name].get("sha256", "-")}'
                for name in sorted(staged.meta)
            ).encode('utf-8')
        ).hexdigest()
        return BundlePreview(
            schema_version=SUPPORT_SCHEMA_VERSION,
            members=member_previews,
            categories=plan.categories,
            excluded_categories=tuple(
                c for c in PackageCategory if c not in plan.categories
            ),
            exclusions=plan.exclusions,
            collection_errors=tuple(staged.collection_errors),
            byte_budget=plan.byte_budget,
            included_bytes=staged.written,
            preview_sha256=digest,
        )

    def build(self, destination: Path, plan: PackagePlan) -> PackageResult:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = self._stage(plan)
        included = staged.included
        skipped = staged.skipped
        members = staged.meta
        written = staged.written

        with _staged_zip_archive(destination) as archive:
            for name, data in staged.members.items():
                # Fixed timestamp: identical member bytes produce an
                # identical archive (#884 determinism requirement).
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)

            manifest = {
                'package': PACKAGE_PREFIX,
                'schema_version': SUPPORT_SCHEMA_VERSION,
                'created_at': _utc_now(),
                'categories': [c.value for c in plan.categories],
                'excluded_categories': [
                    c.value
                    for c in PackageCategory
                    if c not in plan.categories
                ],
                'exclusions': list(plan.exclusions),
                'not_a_backup': True,
                'not_a_project_export': True,
                'included_files': included,
                'skipped_files': skipped,
                'members': members,
                'collection_errors': staged.collection_errors,
                'bytes': written,
                'byte_budget': plan.byte_budget,
                # The budget bounds member payload bytes, not the final
                # archive: ZIP container overhead and manifest.json sit
                # outside it. ``archive_bytes`` (filled post-close) is the
                # authoritative produced size (#749).
                'budget_scope': 'member_payload_bytes',
                'integrity': {
                    name: meta['sha256']
                    for name, meta in members.items()
                    if 'sha256' in meta
                },
            }
            info = zipfile.ZipInfo(
                'manifest.json', date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(manifest, indent=2))

        archive_bytes = destination.stat().st_size
        return PackageResult(
            path=destination,
            bytes_written=archive_bytes,
            included=tuple(included),
            skipped=tuple(skipped),
            manifest_name='manifest.json',
        )

    def _write_launch_metadata(
        self,
        plan: PackagePlan,
        write_json: Callable[[str, Any], None],
        members: dict[str, dict[str, Any]],
        skipped: list[str],
    ) -> None:
        """Bounded launch history for the bundle (#739 recovery metadata).

        The rolling record (build, mode, clean/unclean exit, failure class)
        is the evidence support needs for repeated-startup-failure reports.
        ``last_project_ref`` follows the same privacy rule as the
        project-ids category: redacted unless the user opted in. A corrupt
        metadata file is recorded as skipped, never replaced with an
        empty history that would look like "no launches on record".
        """

        name = 'launch_metadata.json'
        path = diagnostics_dir(self.data_dir) / 'recovery-launch-metadata.json'
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            members[name] = {
                'status': 'skipped',
                'reason': 'unreadable',
                'classification': FieldClassification.SAFE_DIAGNOSTIC.value,
            }
            skipped.append(name)
            return
        if not isinstance(payload, dict):
            members[name] = {
                'status': 'skipped',
                'reason': 'unreadable',
                'classification': FieldClassification.SAFE_DIAGNOSTIC.value,
            }
            skipped.append(name)
            return
        records = payload.get('records')
        if not isinstance(records, list):
            members[name] = {
                'status': 'skipped',
                'reason': 'unreadable',
                'classification': FieldClassification.SAFE_DIAGNOSTIC.value,
            }
            skipped.append(name)
            return
        if not plan.include_project_ids:
            for record in records:
                if isinstance(record, dict) and record.get('last_project_ref'):
                    record['last_project_ref'] = '<set>'
        write_json(name, payload)

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
    'BundleMemberPreview',
    'BundlePreview',
    'CATEGORY_CLASSIFICATION',
    'DATABASE_NAME',
    'DiagnosticPackageBuilder',
    'EnvironmentSummary',
    'FieldClassification',
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
    'capture_receiver_probe',
    'environment_summary',
    'failure_correlation_id',
    'package_filename',
    'previous_session_unexpected_end',
    'redact_support_value',
    'rew_api_probe',
    'run_health_checks',
    'semantic_integrity_check',
    'vtk_probe',
]
