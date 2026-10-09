"""Isolated restore drill for whole-data backups (#992).

A restore drill answers one question honestly: *can this backup actually be
restored* — without ever touching the live data directory. It runs the real
``native_backup.restore_backup`` machinery against a user-chosen sandbox,
then independently re-verifies the landed bytes (SQLite integrity, asset
hashes against the manifest, authority-graph replay, a same-machine open via
the application's own ``SceneRepository`` path, and the schema migration leg
when the archive predates the current schema).

Honesty contract:

- The drill never claims more than it proved. ``claims`` names exactly the
  states the run established (``archive_verified``,
  ``isolated_restore_succeeded``, ``same_machine_opened``);
  ``non_claims`` always carries what a sandbox drill can never prove
  (other-PC migration, physical disaster recovery, operator acceptance).
- The live data directory is fingerprinted before and after via
  ``managed_data_fingerprint``; a changed fingerprint is a *failed* verdict,
  not a footnote.
- Verdict vocabulary: ``restorable`` (every check clean),
  ``restorable_with_conditions`` (restore proven but with declared
  conditions — schema migration needed, degraded declared authorities,
  missing build provenance, or sandbox residue), ``failed`` (a conclusive
  failure — invalid archive, restore error, broken landed bytes),
  ``not_verifiable`` (the environment prevented assessment — unusable
  sandbox, unprobeable free space).
- A drill result never deletes or mutates the backup it inspected, and
  never deletes any other backup either.

The journal (``restore-drill-results.jsonl`` beside the managed data root)
keeps verdicts, fingerprints and check detail — never secrets, never bundle
contents.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from .automatic_backup import managed_data_fingerprint
from .cad_schema import (
    NATIVE_SCHEMA_VERSION,
    native_schema_compatibility,
    read_native_schema_version,
)
from .native_backup import (
    DATABASE_NAME,
    BackupCancelledError,
    BackupError,
    BackupManifest,
    _assert_staged_database_openable,
    _raise_if_backup_cancelled,
    inspect_backup as native_inspect_backup,
    restore_backup as native_restore_backup,
)
from .native_upgrade import execute_native_upgrade


DRILL_RESULTS_FILENAME = 'restore-drill-results.jsonl'
_DRILL_SANDBOX_PREFIX = 'htdt-drill-'
#: The sandbox holds the staged copy plus the landed tree, so free space
#: must cover roughly twice the expanded archive plus headroom.
_SANDBOX_HEADROOM = 64 * 1024 * 1024

DrillVerdict = Literal[
    'restorable',
    'restorable_with_conditions',
    'failed',
    'not_verifiable',
]
DrillCheckStatus = Literal[
    'passed',
    'failed',
    'conditional',
    'unknown',
    'skipped',
]


class RestoreDrillError(RuntimeError):
    """The drill could not run at all (caller/environment precondition)."""


class _DrillConcluded(Exception):
    """Internal early-exit carrying the verdict the run already earned."""

    def __init__(self, verdict: DrillVerdict) -> None:
        super().__init__(verdict)
        self.verdict = verdict


@dataclass(frozen=True)
class RestoreDrillCheck:
    """One verification step's honest outcome."""

    check_id: str
    status: DrillCheckStatus
    detail_ja: str


@dataclass(frozen=True)
class RestoreDrillResult:
    """Complete drill record — also the JSONL journal line payload."""

    drill_id: str
    backup_path: str
    backup_name: str
    verdict: DrillVerdict
    checks: tuple[RestoreDrillCheck, ...]
    claims: tuple[str, ...]
    non_claims: tuple[str, ...]
    manifest_sha256: str | None
    restored_native_schema_version: int | None
    final_native_schema_version: int | None
    scene_count: int | None
    live_fingerprint_before: str
    live_fingerprint_after: str
    sandbox_dir: str
    sandbox_cleaned: bool
    started_at_utc: str
    finished_at_utc: str

    def to_json(self) -> dict:
        return {
            'drill_id': self.drill_id,
            'backup_path': self.backup_path,
            'backup_name': self.backup_name,
            'verdict': self.verdict,
            'checks': [
                {
                    'check_id': check.check_id,
                    'status': check.status,
                    'detail_ja': check.detail_ja,
                }
                for check in self.checks
            ],
            'claims': list(self.claims),
            'non_claims': list(self.non_claims),
            'manifest_sha256': self.manifest_sha256,
            'restored_native_schema_version': (
                self.restored_native_schema_version
            ),
            'final_native_schema_version': self.final_native_schema_version,
            'scene_count': self.scene_count,
            'live_fingerprint_before': self.live_fingerprint_before,
            'live_fingerprint_after': self.live_fingerprint_after,
            'sandbox_dir': self.sandbox_dir,
            'sandbox_cleaned': self.sandbox_cleaned,
            'started_at_utc': self.started_at_utc,
            'finished_at_utc': self.finished_at_utc,
        }

    @classmethod
    def from_json(cls, payload: dict) -> 'RestoreDrillResult':
        return cls(
            drill_id=str(payload['drill_id']),
            backup_path=str(payload['backup_path']),
            backup_name=str(payload['backup_name']),
            verdict=payload['verdict'],
            checks=tuple(
                RestoreDrillCheck(
                    check_id=str(entry['check_id']),
                    status=entry['status'],
                    detail_ja=str(entry['detail_ja']),
                )
                for entry in payload.get('checks', ())
            ),
            claims=tuple(str(c) for c in payload.get('claims', ())),
            non_claims=tuple(str(c) for c in payload.get('non_claims', ())),
            manifest_sha256=payload.get('manifest_sha256'),
            restored_native_schema_version=payload.get(
                'restored_native_schema_version'
            ),
            final_native_schema_version=payload.get(
                'final_native_schema_version'
            ),
            scene_count=payload.get('scene_count'),
            live_fingerprint_before=str(
                payload.get('live_fingerprint_before', '')
            ),
            live_fingerprint_after=str(
                payload.get('live_fingerprint_after', '')
            ),
            sandbox_dir=str(payload.get('sandbox_dir', '')),
            sandbox_cleaned=bool(payload.get('sandbox_cleaned', False)),
            started_at_utc=str(payload.get('started_at_utc', '')),
            finished_at_utc=str(payload.get('finished_at_utc', '')),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024.0 or unit == 'TiB':
            return (
                f'{int(size):,} {unit}'
                if unit == 'B'
                else f'{size:,.1f} {unit}'
            )
        size /= 1024.0
    return f'{value:,} B'


def latest_drill_result(data_dir: Path) -> RestoreDrillResult | None:
    """Most recent journal entry, or None when no drill has been recorded.

    A corrupt tail line degrades to ``None`` honestly — the journal is an
    operator convenience, not an authority.
    """

    journal = Path(data_dir) / DRILL_RESULTS_FILENAME
    if not journal.is_file():
        return None
    try:
        lines = [
            line
            for line in journal.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]
    except OSError:
        return None
    for line in reversed(lines[-8:]):
        try:
            return RestoreDrillResult.from_json(json.loads(line))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _append_journal(data_dir: Path, result: RestoreDrillResult) -> None:
    journal = Path(data_dir) / DRILL_RESULTS_FILENAME
    with journal.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(result.to_json(), ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _assert_sandbox_isolated(
    sandbox_root: Path, live_data_dir: Path
) -> Path:
    """Fail closed unless the sandbox is disjoint from live managed data.

    A sandbox inside the managed root would write into the tree the drill
    claims to leave untouched; the live root inside the sandbox would let a
    cleanup pass delete live data. Both directions refuse.
    """

    sandbox = Path(sandbox_root).resolve()
    live = Path(live_data_dir).resolve()
    if sandbox == live or live.is_relative_to(sandbox):
        raise RestoreDrillError(
            f'sandbox overlaps the live data directory: {sandbox_root}'
        )
    if sandbox.is_relative_to(live):
        raise RestoreDrillError(
            'sandbox must not be inside the live data directory: '
            f'{sandbox_root}'
        )
    try:
        sandbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreDrillError(
            f'sandbox directory cannot be created: {sandbox_root} ({exc})'
        ) from exc
    probe = sandbox / f'.{_DRILL_SANDBOX_PREFIX}probe-{uuid4().hex}'
    try:
        probe.write_text('probe', encoding='ascii')
        probe.unlink()
    except OSError as exc:
        raise RestoreDrillError(
            f'sandbox directory is not writable: {sandbox_root} ({exc})'
        ) from exc
    return sandbox


def _sqlite_integrity(database_path: Path) -> None:
    with closing(
        sqlite3.connect(
            f'file:{database_path.as_posix()}?mode=ro', uri=True
        )
    ) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise BackupError(
                f'SQLite integrity check failed: {integrity!r}'
            )
        foreign_keys = connection.execute(
            'PRAGMA foreign_key_check'
        ).fetchall()
        if foreign_keys:
            raise BackupError(
                f'SQLite foreign-key check failed: {foreign_keys!r}'
            )


def _landed_hashes_ok(
    data_dir: Path, manifest: BackupManifest
) -> tuple[int, str | None]:
    """Re-hash every landed file against the manifest — verifies the swap,
    not just the staged bytes the staging check already covered."""

    verified = 0
    for entry in manifest.files:
        target = data_dir.joinpath(*entry.path.split('/'))
        if not target.is_file():
            return verified, entry.path
        digest = sha256()
        with target.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != entry.sha256:
            return verified, entry.path
        verified += 1
    return verified, None


def _scene_count(database_path: Path) -> int | None:
    """Read-only document-head count; ``None`` when the table does not exist
    in this archive's schema (reported as unknown, never guessed)."""

    try:
        with closing(
            sqlite3.connect(
                f'file:{database_path.as_posix()}?mode=ro', uri=True
            )
        ) as connection:
            present = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='scene_document_heads'"
            ).fetchone()
            if present is None:
                return None
            row = connection.execute(
                'SELECT COUNT(*) FROM scene_document_heads'
            ).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return None


def _audit_restored(
    restored_db: Path,
    manifest: BackupManifest,
    *,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    """Replay the authority graph on a clone of the landed bytes.

    The audit's repository construction may migrate the probed database —
    it runs on a throwaway clone so the restored file stays pinned to the
    manifest hash (same contract as native staging). Only the stale set the
    manifest declared is tolerated.
    """

    from .native_authority_audit import (
        AuthorityAuditError,
        audit_native_authority_graph,
    )

    # The clone must live inside the restored data root: the audit
    # resolves managed-asset relative paths against the DB's parent.
    probe = restored_db.parent / f'.audit-clone-{uuid4().hex}.sqlite3'
    shutil.copyfile(restored_db, probe)
    try:
        report = audit_native_authority_graph(
            probe, is_cancelled=is_cancelled
        )
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    if report.ok:
        return
    declared = {
        (e.authority, e.record_ref, e.failure_class)
        for e in manifest.stale_authorities
    }
    actual = {
        (d.authority, d.record_ref, d.failure_class)
        for d in report.diagnostics
    }
    if report.unclassified_tables or not actual <= declared:
        raise AuthorityAuditError(report)


def run_restore_drill(
    backup_path: Path,
    sandbox_root: Path,
    live_data_dir: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> RestoreDrillResult:
    """Run the isolated restore drill and return its honest verdict.

    ``sandbox_root`` is a user-chosen directory; the drill owns a fresh
    ``htdt-drill-<id>/`` subtree inside it and removes it afterwards.
    ``live_data_dir`` is only fingerprinted, never opened for write.
    ``BackupCancelledError`` propagates so the controller routes the run to
    the cancelled lane. ``RestoreDrillError`` is raised for unusable
    sandboxes before any work starts.
    """

    backup_path = Path(backup_path)
    live_data_dir = Path(live_data_dir)
    started = _utc_now()
    drill_id = uuid4().hex
    checks: list[RestoreDrillCheck] = []
    claims: list[str] = []
    conditions = False
    manifest: BackupManifest | None = None
    staged_schema_version: int | None = None
    final_schema: int | None = None
    scene_count: int | None = None
    sandbox_dir = Path('')
    cleaned = False
    verdict: DrillVerdict | None = None

    def emit(phase: str) -> None:
        if on_phase is not None:
            on_phase(phase)
        _raise_if_backup_cancelled(is_cancelled)

    fingerprint_before = managed_data_fingerprint(live_data_dir)
    sandbox = _assert_sandbox_isolated(sandbox_root, live_data_dir)
    drill_dir = sandbox / f'{_DRILL_SANDBOX_PREFIX}{drill_id[:12]}'
    drill_dir.mkdir(parents=False, exist_ok=False)
    sandbox_dir = drill_dir
    drill_data_dir = drill_dir / 'data'

    try:
        emit('archive')
        if not backup_path.is_file():
            checks.append(
                RestoreDrillCheck(
                    'archive_verified',
                    'failed',
                    'バックアップファイルが見つかりません'
                    f': {backup_path.name}',
                )
            )
            raise _DrillConcluded('failed')
        try:
            manifest, staged_schema_version = native_inspect_backup(
                backup_path, is_cancelled=is_cancelled
            )
        except BackupCancelledError:
            raise
        except Exception as exc:
            checks.append(
                RestoreDrillCheck(
                    'archive_verified',
                    'failed',
                    'アーカイブの検証に失敗しました'
                    '（破損または改竄の可能性）'
                    f': {type(exc).__name__}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'archive_verified',
                'passed',
                f'マニフェスト検証済み（{len(manifest.files)} ファイル、'
                f'作成 {manifest.created_at_utc}）',
            )
        )
        claims.append('archive_verified')

        if manifest.build is not None:
            build = manifest.build
            detail = build.display_version
            if build.commit_sha:
                detail += f'（{build.commit_sha[:8]}）'
            checks.append(
                RestoreDrillCheck(
                    'build_provenance',
                    'passed',
                    f'作成ビルド: {detail}',
                )
            )
        else:
            conditions = True
            checks.append(
                RestoreDrillCheck(
                    'build_provenance',
                    'unknown',
                    '作成ビルド情報はありません（旧形式のアーカイブ）',
                )
            )
        checks.append(
            RestoreDrillCheck(
                'signature',
                'skipped',
                'このバックアップ形式に署名フィールドはありません'
                '（マニフェスト SHA-256 とファイル別ハッシュで完全性を'
                '検証済み）',
            )
        )

        compatibility = native_schema_compatibility(staged_schema_version)
        if compatibility == 'current':
            checks.append(
                RestoreDrillCheck(
                    'schema_compatibility',
                    'passed',
                    f'データスキーマ v{staged_schema_version}（現行）',
                )
            )
        elif compatibility == 'incompatible_newer':
            checks.append(
                RestoreDrillCheck(
                    'schema_compatibility',
                    'failed',
                    f'データスキーマ v{staged_schema_version} はこのビルド'
                    f'（v{NATIVE_SCHEMA_VERSION}）より新しいため開けません',
                )
            )
            raise _DrillConcluded('failed')
        else:
            conditions = True
            label = (
                'バージョン情報なし（従来形式）'
                if compatibility == 'legacy_unversioned'
                else f'v{staged_schema_version}'
            )
            checks.append(
                RestoreDrillCheck(
                    'schema_compatibility',
                    'conditional',
                    f'データスキーマ {label} — 復元時に '
                    f'v{NATIVE_SCHEMA_VERSION} への移行が必要です',
                )
            )

        if manifest.degraded:
            conditions = True
            checks.append(
                RestoreDrillCheck(
                    'archive_degraded',
                    'conditional',
                    '宣言済みの未検証権威レコード '
                    f'{len(manifest.stale_authorities)} 件を含む'
                    'アーカイブです',
                )
            )

        needed = (
            sum(entry.size_bytes for entry in manifest.files) * 2
            + _SANDBOX_HEADROOM
        )
        try:
            free = shutil.disk_usage(sandbox).free
        except OSError:
            checks.append(
                RestoreDrillCheck(
                    'disk_space',
                    'unknown',
                    'サンドボックスの空き容量を測定できませんでした',
                )
            )
            raise _DrillConcluded('not_verifiable')
        if free < needed:
            checks.append(
                RestoreDrillCheck(
                    'disk_space',
                    'failed',
                    f'空き容量不足: 必要 {_format_bytes(needed)} / '
                    f'利用可能 {_format_bytes(free)}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'disk_space',
                'passed',
                f'十分な空き容量（必要 {_format_bytes(needed)} / '
                f'利用可能 {_format_bytes(free)}）',
            )
        )

        emit('restore')
        try:
            native_restore_backup(
                drill_data_dir,
                backup_path,
                is_cancelled=is_cancelled,
            )
        except BackupCancelledError:
            raise
        except Exception as exc:
            checks.append(
                RestoreDrillCheck(
                    'isolated_restore',
                    'failed',
                    '隔離ディレクトリへの復元に失敗しました'
                    f': {type(exc).__name__}: {exc}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'isolated_restore',
                'passed',
                '本物の復元機構が隔離ディレクトリへの復元を完了しました',
            )
        )
        claims.append('isolated_restore_succeeded')

        emit('verify')
        restored_db = drill_data_dir / DATABASE_NAME
        try:
            _sqlite_integrity(restored_db)
        except Exception as exc:
            checks.append(
                RestoreDrillCheck(
                    'sqlite_integrity',
                    'failed',
                    '復元後データベースの整合性チェックに失敗'
                    f': {exc}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'sqlite_integrity',
                'passed',
                '復元後データベースの整合性チェックに合格しました',
            )
        )

        verified_count, mismatch = _landed_hashes_ok(
            drill_data_dir, manifest
        )
        if mismatch is not None:
            checks.append(
                RestoreDrillCheck(
                    'landed_hashes',
                    'failed',
                    '復元後ファイルのハッシュ不一致または欠落'
                    f': {mismatch}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'landed_hashes',
                'passed',
                f'復元された全 {verified_count} ファイルがマニフェストの'
                'ハッシュと一致しました',
            )
        )

        try:
            _assert_staged_database_openable(restored_db)
        except Exception as exc:
            checks.append(
                RestoreDrillCheck(
                    'same_machine_opened',
                    'failed',
                    '復元データをこのアプリのオープン経路で開けません'
                    f': {exc}',
                )
            )
            raise _DrillConcluded('failed')
        checks.append(
            RestoreDrillCheck(
                'same_machine_opened',
                'passed',
                'このアプリの実際のオープン経路（スキーマ確認 + '
                'SceneRepository）で復元データを開けました',
            )
        )
        claims.append('same_machine_opened')

        try:
            _audit_restored(
                restored_db,
                manifest,
                is_cancelled=is_cancelled,
            )
            checks.append(
                RestoreDrillCheck(
                    'authority_audit',
                    'passed',
                    '権威グラフの再検証に合格しました'
                    '（既知の診断は全てマニフェスト宣言済み）',
                )
            )
        except BackupCancelledError:
            raise
        except Exception as exc:
            checks.append(
                RestoreDrillCheck(
                    'authority_audit',
                    'failed',
                    f'権威グラフの再検証に失敗: {type(exc).__name__}: {exc}',
                )
            )
            raise _DrillConcluded('failed')

        scene_count = _scene_count(restored_db)
        checks.append(
            RestoreDrillCheck(
                'scene_count',
                'passed' if scene_count is not None else 'unknown',
                (
                    f'プロジェクトのシーン記録 {scene_count} 件を'
                    '読み取り専用で確認しました'
                    if scene_count is not None
                    else 'このスキーマには scene_document_heads テーブルが'
                    'ありません（旧形式）'
                ),
            )
        )

        if compatibility in ('migration_required', 'legacy_unversioned'):
            emit('migrate')
            try:
                event = execute_native_upgrade(drill_data_dir)
            except Exception as exc:
                checks.append(
                    RestoreDrillCheck(
                        'schema_migration',
                        'failed',
                        '隔離環境でのスキーマ移行に失敗'
                        f': {type(exc).__name__}: {exc}',
                    )
                )
                raise _DrillConcluded('failed')
            try:
                final_schema = read_native_schema_version(restored_db)
                _sqlite_integrity(restored_db)
            except Exception as exc:
                checks.append(
                    RestoreDrillCheck(
                        'schema_migration',
                        'failed',
                        f'移行後のデータベース検証に失敗: {exc}',
                    )
                )
                raise _DrillConcluded('failed')
            checks.append(
                RestoreDrillCheck(
                    'schema_migration',
                    'conditional',
                    f'隔離環境で v{staged_schema_version} → '
                    f'v{final_schema} へ移行できました'
                    f'（イベント {event.upgrade_id[:8]}）— '
                    '本番復元でも同じ移行が必要です',
                )
            )
    except _DrillConcluded as concluded:
        verdict = concluded.verdict
    finally:
        try:
            shutil.rmtree(drill_dir)
        except OSError:
            cleaned = False
        else:
            cleaned = True

    # Live-untouched is checked after every phase so even an early exit
    # still reports the fingerprint comparison honestly.
    fingerprint_after = managed_data_fingerprint(live_data_dir)
    if fingerprint_after != fingerprint_before:
        checks.append(
            RestoreDrillCheck(
                'live_data_untouched',
                'failed',
                '演習中に本番データの指紋が変化しました — '
                '隔離が破れた可能性があります',
            )
        )
        verdict = 'failed'
    else:
        checks.append(
            RestoreDrillCheck(
                'live_data_untouched',
                'passed',
                '本番データの指紋は演習前後で一致しています',
            )
        )

    if not cleaned:
        checks.append(
            RestoreDrillCheck(
                'sandbox_cleanup',
                'conditional',
                '演習用サンドボックスの完全な削除に失敗しました'
                f'（残存: {sandbox_dir}）',
            )
        )
        conditions = True
    else:
        checks.append(
            RestoreDrillCheck(
                'sandbox_cleanup',
                'passed',
                '演習用サンドボックスを完全に削除しました',
            )
        )

    if verdict is None:
        verdict = (
            'restorable_with_conditions' if conditions else 'restorable'
        )

    result = RestoreDrillResult(
        drill_id=drill_id,
        backup_path=str(backup_path),
        backup_name=backup_path.name,
        verdict=verdict,
        checks=tuple(checks),
        claims=tuple(claims),
        non_claims=(
            'other_pc_migration',
            'physical_disaster_recovery',
            'operator_acceptance',
        ),
        manifest_sha256=(
            manifest.manifest_sha256 if manifest is not None else None
        ),
        restored_native_schema_version=staged_schema_version,
        final_native_schema_version=final_schema,
        scene_count=scene_count,
        live_fingerprint_before=fingerprint_before,
        live_fingerprint_after=fingerprint_after,
        sandbox_dir=str(sandbox_dir),
        sandbox_cleaned=cleaned,
        started_at_utc=started,
        finished_at_utc=_utc_now(),
    )
    try:
        _append_journal(live_data_dir, result)
    except OSError:
        pass
    return result


__all__ = [
    'DRILL_RESULTS_FILENAME',
    'RestoreDrillCheck',
    'RestoreDrillError',
    'RestoreDrillResult',
    'latest_drill_result',
    'run_restore_drill',
]
