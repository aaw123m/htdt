"""Managed data relocation (#621): move the live root to another drive.

The HTDT data directory holds the live database and managed evidence; users
with nearly-full drives need a supported way to move that root instead of
hand-copying it and breaking every authority invariant.

This module owns three things:

1. ``HTDTBootstrapConfig`` — the pointer from the user profile to the
   *current* managed root. It lives OUTSIDE the managed root itself so it
   survives the move; startup precedence is explicit ``--data-dir`` >
   bootstrap config > platform default. A configured-but-unavailable root
   is a first-class error, never a silent fall-back to the default.
2. ``plan_data_relocation`` — read-only preflight: byte estimates, free
   space, and the hard blockers (destination inside source, non-empty
   destination, source in use, ...).
3. ``execute_data_relocation`` — copy → verify → cutover. The source is
   kept, intact, until the staged copy at the destination has been
   verified; it is then parked as ``<source>.relocated-<stamp>`` rather
   than deleted, so nothing is unrecoverable.

Relocation runs on a quiesced data directory — the caller must not hold
the instance lock and no other HTDT process may.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .managed_assets import MANAGED_ASSETS_DIRNAME, sha256_file
from .native_backup import DATABASE_NAME, recover_interrupted_restore
from .persisted_data import relocation_carried_components
from .runtime_instance import SingleInstanceGuard


_LOGGER = logging.getLogger('htdt.native')

BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_FILENAME = 'htdt-bootstrap.json'
RELOCATED_SUFFIX = '.relocated-'


class ManagedDataUnavailableError(RuntimeError):
    """The configured managed data root is not usable right now."""


class DataRelocationError(RuntimeError):
    pass


class DataRelocationBlockedError(DataRelocationError):
    pass


class HTDTBootstrapConfig(BaseModel):
    """The persistent pointer to the managed data root.

    Stored outside the managed root (``%LOCALAPPDATA%\\HTDT\\htdt-bootstrap.json``
    on Windows, ``~/.htdt/htdt-bootstrap.json`` elsewhere) so it survives
    the move it describes.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BOOTSTRAP_SCHEMA_VERSION
    data_dir: str = Field(min_length=1)
    written_at_utc: str = Field(min_length=1)

    @field_validator('data_dir')
    @classmethod
    def _no_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('data_dir must be non-empty')
        return value


def bootstrap_config_path() -> Path:
    """Where the bootstrap pointer lives — outside any managed root."""

    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'HTDT' / BOOTSTRAP_FILENAME
    return Path.home() / '.htdt' / BOOTSTRAP_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_bootstrap_config(
    path: Path | None = None,
) -> HTDTBootstrapConfig | None:
    path = path or bootstrap_config_path()
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return HTDTBootstrapConfig.model_validate(payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _LOGGER.warning('unreadable bootstrap config %s: %s', path, exc)
        return None


def save_bootstrap_config(
    config: HTDTBootstrapConfig, path: Path | None = None
) -> Path:
    path = path or bootstrap_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temp.write_text(
        json.dumps(config.model_dump(mode='json'), indent=2, sort_keys=True),
        encoding='utf-8',
    )
    os.replace(temp, path)
    return path


def resolve_data_dir(
    explicit: Path | None,
    *,
    bootstrap_path: Path | None = None,
    default: Path | None = None,
) -> tuple[Path, Literal['explicit', 'bootstrap', 'default']]:
    """Resolve the managed root: ``--data-dir`` > bootstrap > default."""

    if explicit is not None:
        return Path(explicit), 'explicit'
    config = load_bootstrap_config(bootstrap_path)
    if config is not None:
        return Path(config.data_dir), 'bootstrap'
    if default is None:
        from .runtime_instance import default_data_dir

        default = default_data_dir()
    return Path(default), 'default'


def assert_managed_root_available(
    root: Path, source: Literal['explicit', 'bootstrap', 'default']
) -> None:
    """Fail closed when the resolved root does not exist.

    An explicit path may create a new directory. A bootstrap-configured
    root that has gone missing (drive unplugged, directory moved) is an
    error — silently opening the default instead would look like data loss.
    """

    root = Path(root)
    if source == 'bootstrap' and not root.is_dir():
        raise ManagedDataUnavailableError(
            f'HTDT data directory {root} (from the bootstrap configuration) '
            'is not available. The drive may be disconnected or the data '
            'may have been moved. Reconnect the drive, restore the '
            'directory, or start HTDT with an explicit --data-dir.'
        )


@dataclass(frozen=True)
class RelocationBlocker:
    kind: str
    detail: str


@dataclass(frozen=True)
class ManagedDataRelocationPlan:
    source_dir: Path
    destination_dir: Path
    database_bytes: int
    asset_count: int
    asset_bytes: int
    total_bytes: int
    free_space_bytes: int | None
    blockers: tuple[RelocationBlocker, ...]

    @property
    def executable(self) -> bool:
        return not self.blockers


def _tree_stats(assets_root: Path) -> tuple[int, int]:
    if not assets_root.is_dir():
        return 0, 0
    count = 0
    total = 0
    for candidate in assets_root.iterdir():
        try:
            if candidate.is_file():
                count += 1
                total += candidate.stat().st_size
        except OSError:
            continue
    return count, total


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False


def _destination_has_live_data(destination: Path) -> bool:
    database = destination / DATABASE_NAME
    return database.is_file() and database.stat().st_size > 0


def plan_data_relocation(
    source_dir: Path, destination_dir: Path
) -> ManagedDataRelocationPlan:
    """Read-only preflight for moving the managed root."""

    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)
    database = source_dir / DATABASE_NAME
    database_bytes = 0
    if database.is_file():
        try:
            database_bytes = database.stat().st_size
        except OSError:
            pass
    asset_count, asset_bytes = _tree_stats(
        source_dir / MANAGED_ASSETS_DIRNAME
    )
    # Root-tied registry components (preferences, history, recovery
    # generations, ...) are carried too — account for their bytes so the
    # preflight free-space estimate is not silently optimistic (#769).
    carried_bytes = 0
    for component in relocation_carried_components():
        carried = source_dir / component.path
        if component.is_directory:
            _count, carried = _tree_stats(carried)
            carried_bytes += carried
        elif carried.is_file():
            try:
                carried_bytes += carried.stat().st_size
            except OSError:
                pass
    total = database_bytes + asset_bytes + carried_bytes

    free: int | None = None
    probe = (
        destination_dir
        if destination_dir.is_dir()
        else destination_dir.parent
    )
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        pass

    blockers: list[RelocationBlocker] = []
    if not database.is_file():
        blockers.append(
            RelocationBlocker(
                'source_has_no_data',
                f'{source_dir} does not contain a live HTDT database',
            )
        )
    if source_dir.resolve() == destination_dir.resolve():
        blockers.append(
            RelocationBlocker(
                'same_location',
                'destination is the current data directory',
            )
        )
    if _is_within(destination_dir, source_dir):
        blockers.append(
            RelocationBlocker(
                'destination_inside_source',
                'destination is inside the managed data directory',
            )
        )
    if _is_within(source_dir, destination_dir):
        blockers.append(
            RelocationBlocker(
                'source_inside_destination',
                'the managed data directory is inside the destination',
            )
        )
    if destination_dir.exists() and any(destination_dir.iterdir()):
        if _destination_has_live_data(destination_dir):
            blockers.append(
                RelocationBlocker(
                    'destination_has_live_data',
                    f'{destination_dir} already contains a live HTDT '
                    'database; choose an empty destination',
                )
            )
        else:
            blockers.append(
                RelocationBlocker(
                    'destination_not_empty',
                    f'{destination_dir} is not empty; choose an empty '
                    'destination',
                )
            )
    if free is not None and free < total:
        blockers.append(
            RelocationBlocker(
                'insufficient_space',
                f'destination needs ~{total} bytes, only {free} free',
            )
        )
    return ManagedDataRelocationPlan(
        source_dir=source_dir,
        destination_dir=destination_dir,
        database_bytes=database_bytes,
        asset_count=asset_count,
        asset_bytes=asset_bytes,
        total_bytes=total,
        free_space_bytes=free,
        blockers=tuple(blockers),
    )


def _verify_staged_root(staged: Path) -> None:
    """Verification phase: the copied root must be a valid data directory."""

    database = staged / DATABASE_NAME
    if not database.is_file():
        raise DataRelocationError(
            f'staged copy is missing {DATABASE_NAME}'
        )
    with closing(sqlite3.connect(database)) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise DataRelocationError(
                f'staged database integrity check failed: {integrity!r}'
            )
        # Re-assert the source asset contract on the staged copy: every
        # asset row must have a copied file with the recorded digest.
        for table, sha_column in (
            ('cad_measurement_assets', 'sha256'),
            ('cad_quality_calibration_files', 'sha256'),
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            rows = connection.execute(
                f'SELECT {sha_column}, relative_path, size_bytes FROM {table}'
            ).fetchall()
            for sha, relative_path, size_bytes in rows:
                candidate = staged / str(relative_path)
                if not candidate.is_file():
                    raise DataRelocationError(
                        f'staged asset missing: {relative_path}'
                    )
                if candidate.stat().st_size != int(size_bytes):
                    raise DataRelocationError(
                        f'staged asset size mismatch: {relative_path}'
                    )
                if sha256_file(candidate) != str(sha):
                    raise DataRelocationError(
                        f'staged asset digest mismatch: {relative_path}'
                    )


def execute_data_relocation(
    source_dir: Path,
    destination_dir: Path,
    *,
    bootstrap_path: Path | None = None,
) -> tuple[ManagedDataRelocationPlan, Path]:
    """Copy → verify → cutover the managed root.

    Returns (plan, parked_source) — the old root is renamed to
    ``<source>.relocated-<stamp>`` after the new location verifies, never
    deleted. Raises ``DataRelocationBlockedError`` when preflight found
    blockers, or ``DataRelocationError`` mid-flight (with the staged copy
    left for inspection and the source untouched).
    """

    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)

    # The destination must be reachable before any data moves.
    plan = plan_data_relocation(source_dir, destination_dir)
    if not plan.executable:
        raise DataRelocationBlockedError(
            'relocation is blocked: '
            + '; '.join(b.detail for b in plan.blockers)
        )

    # Refuse to move a live data directory: acquire the instance lock so a
    # running HTDT is a hard blocker, not a torn copy.
    guard = SingleInstanceGuard(source_dir)
    if not guard.acquire():
        raise DataRelocationBlockedError(
            'relocation is blocked: the data directory is in use by a '
            'running HTDT process'
        )
    staged = destination_dir.with_name(
        f'{destination_dir.name}.relocation-{uuid4().hex[:8]}'
    )
    parked: Path | None = None
    try:
        # Resolve any interrupted restore before copying so the moved tree
        # is the resolved generation, not a mid-swap artifact.
        recover_interrupted_restore(source_dir)

        # ---- copy phase -------------------------------------------------
        staged.mkdir(parents=True, exist_ok=True)
        database = source_dir / DATABASE_NAME
        staged_database = staged / DATABASE_NAME
        with closing(sqlite3.connect(database)) as source, closing(
            sqlite3.connect(staged_database)
        ) as destination:
            source.backup(destination)
            destination.commit()
        live_assets = source_dir / MANAGED_ASSETS_DIRNAME
        staged_assets = staged / MANAGED_ASSETS_DIRNAME
        if live_assets.is_dir():
            staged_assets.mkdir(exist_ok=True)
            for candidate in live_assets.iterdir():
                if candidate.is_file():
                    shutil.copy2(candidate, staged_assets / candidate.name)

        # Registry-carried root components: preferences, library metadata,
        # operational history/state, upgrade journal + recovery generations
        # and diagnostics move with the managed root so nothing root-tied
        # silently resets at the new location (#769). Transient components
        # (runtime.json, instance locks) are never copied.
        for component in relocation_carried_components():
            source_path = source_dir / component.path
            staged_path = staged / component.path
            if component.is_directory:
                if source_path.is_dir():
                    shutil.copytree(source_path, staged_path)
            elif source_path.is_file():
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, staged_path)

        # ---- verify phase -----------------------------------------------
        _verify_staged_root(staged)

        # ---- cutover ------------------------------------------------------
        # Windows cannot rename a directory while a handle inside it is
        # open, so the instance lock is released before either rename. The
        # order keeps a crash recoverable: promote the verified staged copy
        # first, then park the source — at every instant at least one
        # complete root exists.
        guard.release()
        parked = source_dir.with_name(
            f'{source_dir.name}{RELOCATED_SUFFIX}'
            f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
        )
        destination_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination_dir)
        try:
            os.replace(source_dir, parked)
        except OSError as exc:
            raise DataRelocationError(
                f'the verified copy is live at {destination_dir}, but the '
                f'old source directory {source_dir} could not be parked: '
                f'{exc}. Resolve the duplicate manually.'
            ) from exc

        # Point the bootstrap config at the new root.
        save_bootstrap_config(
            HTDTBootstrapConfig(
                data_dir=str(destination_dir),
                written_at_utc=_utc_now(),
            ),
            bootstrap_path,
        )
        _LOGGER.info(
            'managed data relocated: %s -> %s (source parked at %s)',
            source_dir,
            destination_dir,
            parked,
        )
        return plan, parked
    except Exception:
        # Copy/verify failure leaves the source exactly as it was; the
        # staged copy is removed only if it was never promoted.
        if staged.is_dir() and staged != destination_dir and not destination_dir.exists():
            shutil.rmtree(staged, ignore_errors=True)
        raise
    finally:
        guard.release()


__all__ = [
    'DataRelocationBlockedError',
    'DataRelocationError',
    'HTDTBootstrapConfig',
    'ManagedDataRelocationPlan',
    'ManagedDataUnavailableError',
    'RelocationBlocker',
    'assert_managed_root_available',
    'bootstrap_config_path',
    'execute_data_relocation',
    'load_bootstrap_config',
    'plan_data_relocation',
    'resolve_data_dir',
    'save_bootstrap_config',
]
