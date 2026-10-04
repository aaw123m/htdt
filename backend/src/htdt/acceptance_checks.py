"""Auto-check executors for guided acceptance runs (REV48-HWGUIDE).

Every check here really executes against the machine: REW probes go through
the real ``RewApiClient``, DB checks read the canonical authority database,
subprocess checks run the repo's own harnesses. A check is *fail-closed* —
a missing script, an unreachable REW, or a refused query yields
``unavailable``/``fail`` verdicts, never a silent pass. Verdict vocabulary:

* ``pass`` — the check ran and its criterion held;
* ``fail`` — the check ran and its criterion did not hold;
* ``unavailable`` — the check could not run (dependency missing, REW down);
* ``deferred`` — the check recorded state and must be re-run later
  (e.g. after an app restart) to complete.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .canonical_json import canonical_json
from .clock import utc_now_iso

CheckVerdict = Literal['pass', 'fail', 'unavailable', 'deferred']

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AutoCheckResult:
    verdict: CheckVerdict
    detail_ja: str
    #: Machine-readable captured evidence (goes into step.check_detail).
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckContext:
    """What a check may observe — injected so tests can mock every edge."""

    data_dir: Path
    db_path: Path
    rew_base_url: str
    run_id: str
    step_id: str
    input_value: str = ''
    #: Prior step.check_detail — lets two-phase checks resume.
    prior_detail: dict[str, Any] | None = None
    #: Repository handle for run-internal queries (evidence, other runs).
    repository: Any = None
    #: Test seam: inject a fake RewApiClient.
    rew_client: Any = None
    #: Test seam: injected env snapshot / subprocess runner.
    environment: dict[str, Any] | None = None
    subprocess_runner: Callable[..., Any] = subprocess.run


def _repo_root() -> Path:
    return _REPO_ROOT


def _rew_client(ctx: CheckContext):
    if ctx.rew_client is not None:
        return ctx.rew_client
    from .rew_api import RewApiClient

    return RewApiClient(ctx.rew_base_url)


def _query_scalar(ctx: CheckContext, sql: str, params: tuple = ()):
    import sqlite3

    uri = f'file:{ctx.db_path.as_posix()}?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute(sql, params).fetchone()


# ----------------------------------------------------------------------
# environment


def check_env_snapshot(ctx: CheckContext, arg: str) -> AutoCheckResult:
    if ctx.environment is not None:
        snapshot = dict(ctx.environment)
    else:
        snapshot = {
            'hostname': platform.node(),
            'platform': platform.platform(),
            'machine': platform.machine(),
            'python': sys.version.split()[0],
            'time_utc': utc_now_iso(),
        }
        try:
            import htdt

            snapshot['htdt_version'] = getattr(htdt, '__version__', None)
        except Exception:
            snapshot['htdt_version'] = None
        try:
            import PySide6

            snapshot['pyside6'] = PySide6.__version__
        except Exception:
            snapshot['pyside6'] = None
        try:
            from PySide6.QtGui import QGuiApplication

            gui = QGuiApplication.instance()
            if gui is not None:
                snapshot['screens'] = [
                    {
                        'name': screen.name(),
                        'geometry': list(screen.geometry().getRect()),
                        'device_pixel_ratio': screen.devicePixelRatio(),
                    }
                    for screen in gui.screens()
                ]
        except Exception:
            pass
        # The code state under test — a verifier's replay anchor. Repo
        # checkouts expose HEAD; packaged installs record nothing.
        try:
            git = shutil.which('git')
            if git:
                proc = ctx.subprocess_runner(
                    [git, '-C', str(_repo_root()), 'rev-parse', 'HEAD'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode == 0:
                    snapshot['code_sha'] = proc.stdout.strip()
        except Exception:
            pass
    return AutoCheckResult(
        verdict='pass',
        detail_ja='実行環境の証跡を記録しました。',
        evidence={'environment': snapshot},
    )


# ----------------------------------------------------------------------
# REW probes


def check_rew_engine_probe(ctx: CheckContext, arg: str) -> AutoCheckResult:
    status = _rew_client(ctx).status()
    if not status.get('connected'):
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=(
                'REW API(port 4735)に接続できません。REWを起動し、'
                'Preferencesで読取専用APIを有効にしてください。'
            ),
            evidence={'status': status},
        )
    return AutoCheckResult(
        verdict='pass',
        detail_ja=(
            f"REW接続OK — version={status.get('rew_version')}, "
            f"measurements={status.get('measurement_count')}"
        ),
        evidence={'status': status},
    )


def _preflight_or_unavailable(ctx: CheckContext) -> tuple[dict | None, AutoCheckResult | None]:
    try:
        preflight = _rew_client(ctx).get_audio_preflight()
    except Exception as exc:
        return None, AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'REW audioプリフライトを取得できません: {exc}',
            evidence={'error': f'{type(exc).__name__}: {exc}'},
        )
    return preflight, None


def check_rew_audio_ready(ctx: CheckContext, arg: str) -> AutoCheckResult:
    preflight, failure = _preflight_or_unavailable(ctx)
    if failure is not None:
        return failure
    java = preflight.get('java') or {}
    if preflight.get('audio_ready') and java.get('input_endpoint_ready'):
        return AutoCheckResult(
            verdict='pass',
            detail_ja='REWのオーディオ入力は計測可能な状態です。',
            evidence={'preflight': preflight},
        )
    return AutoCheckResult(
        verdict='fail',
        detail_ja=(
            'REWのオーディオ入力が計測可能な状態ではありません '
            f"(audio_ready={preflight.get('audio_ready')}, "
            f"input_endpoint_ready={java.get('input_endpoint_ready')})。"
        ),
        evidence={'preflight': preflight},
    )


def check_rew_input_ready(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """UMIK-1 @48kHz + 校正ファイル選択済み (WINDOWS_ACCEPTANCE §2 手順2)."""
    preflight, failure = _preflight_or_unavailable(ctx)
    if failure is not None:
        return failure
    java = preflight.get('java') or {}
    device = str(java.get('input_device') or '')
    sample_rate = preflight.get('sample_rate_hz')
    cal_present = bool(java.get('input_cal_file_present'))
    problems: list[str] = []
    if 'umik' not in device.lower():
        problems.append(f'入力デバイスがUMIK-1ではありません: {device or "未選択"}')
    if sample_rate != 48000:
        problems.append(f'サンプルレートが48kHzではありません: {sample_rate}')
    if not cal_present:
        problems.append('校正ファイルが選択されていません')
    verdict: CheckVerdict = 'pass' if not problems else 'fail'
    detail = (
        '入力デバイス・48kHz・校正ファイルを確認しました。'
        if not problems
        else ' / '.join(problems)
    )
    return AutoCheckResult(
        verdict=verdict, detail_ja=detail, evidence={'preflight': preflight}
    )


def check_rew_calibration_selected(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """Auto-capture for the orientation step: prove a cal file is selected
    and record its path (the 90° pick itself is human-confirmed)."""
    preflight, failure = _preflight_or_unavailable(ctx)
    if failure is not None:
        return failure
    java = preflight.get('java') or {}
    cal_file = java.get('input_cal_file')
    if cal_file:
        return AutoCheckResult(
            verdict='pass',
            detail_ja=f'校正ファイルが選択されています: {cal_file}',
            evidence={
                'input_cal_file': cal_file,
                'input_device': java.get('input_device'),
            },
        )
    return AutoCheckResult(
        verdict='fail',
        detail_ja='REWで校正ファイルが選択されていません。',
        evidence={'input_cal_file': None},
    )


def check_rew_output_mapping(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """Auto-capture routing evidence: the live REW output channel mapping."""
    preflight, failure = _preflight_or_unavailable(ctx)
    if failure is not None:
        return failure
    java = preflight.get('java') or {}
    mapping = java.get('output_channel_mapping') or []
    verdict: CheckVerdict = 'pass' if mapping else 'fail'
    detail = (
        f'出力マッピングを記録しました（{len(mapping)}チャンネル）。'
        if mapping
        else 'REWの出力チャンネルマッピングが空です。'
    )
    return AutoCheckResult(
        verdict=verdict,
        detail_ja=detail,
        evidence={
            'output_device': java.get('output_device'),
            'output_channel_mapping': mapping,
            'multichannel_ready': java.get('multichannel_ready'),
        },
    )


def check_rew_measurement_count(ctx: CheckContext, arg: str) -> AutoCheckResult:
    minimum = int(arg or 0)
    status = _rew_client(ctx).status()
    if not status.get('connected'):
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja='REW APIに接続できません。',
            evidence={'status': status},
        )
    count = status.get('measurement_count')
    verdict: CheckVerdict = (
        'pass' if isinstance(count, int) and count >= minimum else 'fail'
    )
    detail = (
        f'REWの測定数: {count}（基準: {minimum}以上）'
        if verdict == 'pass'
        else f'REWの測定数が不足しています: {count} < {minimum}'
    )
    return AutoCheckResult(
        verdict=verdict, detail_ja=detail, evidence={'status': status}
    )


# ----------------------------------------------------------------------
# persistence / authority checks


def check_persistence_probe(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """Two-phase restart probe: first run records a digest-bound marker
    evidence row and defers; after the app restarts and the step runs again,
    the marker must still read back byte-identical."""
    if ctx.repository is None:
        return AutoCheckResult(
            verdict='unavailable', detail_ja='リポジトリ参照がありません。'
        )
    existing = [
        ref
        for ref in ctx.repository.evidence_for(ctx.run_id, ctx.step_id)
        if ref.kind == 'persistence_probe'
    ]
    if not existing:
        token = canonical_json(
            {
                'run_id': ctx.run_id,
                'step_id': ctx.step_id,
                'issued_at_utc': utc_now_iso(),
            }
        ).encode('utf-8')
        ctx.repository.attach_evidence(
            ctx.run_id,
            ctx.step_id,
            kind='persistence_probe',
            filename='persistence-probe.json',
            payload=token,
        )
        return AutoCheckResult(
            verdict='deferred',
            detail_ja=(
                '永続化プローブを記録しました。HTDTを再起動し、この実行を'
                '再開してから再度「チェック実行」を押してください。'
            ),
            evidence={'probe_recorded': True},
        )
    ref = existing[0]
    content = ctx.repository.asset_store.read_verified(ref.sha256)
    if content is None:
        return AutoCheckResult(
            verdict='fail',
            detail_ja='再起動前に記録したプローブ証跡が見つかりません。',
        )
    marker = json.loads(content.decode('utf-8'))
    if marker.get('run_id') != ctx.run_id:
        return AutoCheckResult(
            verdict='fail',
            detail_ja='プローブ証跡のrun_idが一致しません。',
        )
    return AutoCheckResult(
        verdict='pass',
        detail_ja=(
            '再起動後にプローブ証跡を検証しました — '
            'Project/証跡データが再現されています。'
        ),
        evidence={
            'probe_sha256': ref.sha256,
            'probe_issued_at_utc': marker.get('issued_at_utc'),
            'verified_after_restart': True,
        },
    )


def check_backup_restore_roundtrip(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """Real roundtrip: backup the live data dir, validate the archive,
    restore it into a scratch data dir and probe the restored authority."""
    from .native_backup import (
        BackupError,
        create_backup,
        restore_backup,
        validate_backup,
    )

    work_root = Path(
        tempfile.mkdtemp(prefix='htdt-acceptance-backup-')
    )
    try:
        backup_path = work_root / 'acceptance-check.htdt-backup'
        manifest = create_backup(ctx.data_dir, backup_path)
        validate_backup(backup_path)
        restore_dir = work_root / 'restored'
        restore_backup(restore_dir, backup_path)
        restored_db = restore_dir / 'cad-scenes.sqlite3'
        if not restored_db.is_file():
            return AutoCheckResult(
                verdict='fail',
                detail_ja='復元先にcad-scenes.sqlite3が存在しません。',
                evidence={'manifest_entries': len(manifest.files)},
            )
        import sqlite3

        with closing(
            sqlite3.connect(
                f'file:{restored_db.as_posix()}?mode=ro', uri=True
            )
        ) as connection:
            integrity = connection.execute(
                'PRAGMA integrity_check'
            ).fetchall()
            run_row = connection.execute(
                'SELECT COUNT(*) FROM htdt_acceptance_runs WHERE run_id=?',
                (ctx.run_id,),
            ).fetchone()
        integrity_ok = integrity == [('ok',)]
        run_present = bool(run_row and run_row[0] >= 1)
        verdict: CheckVerdict = (
            'pass' if integrity_ok and run_present else 'fail'
        )
        return AutoCheckResult(
            verdict=verdict,
            detail_ja=(
                'バックアップ→復元の往復を検証しました'
                f'（integrity={integrity_ok}, この実行の行={run_present}）。'
                if verdict == 'pass'
                else f'復元検証に失敗しました（integrity={integrity_ok}, '
                f'この実行の行={run_present}）。'
            ),
            evidence={
                'manifest_sha256': manifest.manifest_sha256,
                'entries': len(manifest.files),
                'integrity_ok': integrity_ok,
                'run_present_in_restore': run_present,
            },
        )
    except BackupError as exc:
        return AutoCheckResult(
            verdict='fail',
            detail_ja=f'バックアップ検証に失敗しました: {exc}',
            evidence={'error': str(exc)},
        )
    except Exception as exc:
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'バックアップ検証を実行できません: {exc}',
            evidence={'error': f'{type(exc).__name__}: {exc}'},
        )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def check_campaign_registered(ctx: CheckContext, arg: str) -> AutoCheckResult:
    try:
        row = _query_scalar(
            ctx,
            'SELECT COUNT(*) FROM cad_validation_campaign_registrations',
        )
    except Exception as exc:
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'キャンペーン登録を確認できません: {exc}',
        )
    count = int(row[0]) if row else 0
    return AutoCheckResult(
        verdict='pass' if count >= 1 else 'fail',
        detail_ja=(
            f'登録済み検証キャンペーン: {count}件。'
            if count >= 1
            else '検証キャンペーンが未登録です。'
        ),
        evidence={'campaign_registration_count': count},
    )


def check_comparison_present(ctx: CheckContext, arg: str) -> AutoCheckResult:
    try:
        row = _query_scalar(
            ctx, 'SELECT COUNT(*) FROM cad_design_comparison_sets'
        )
    except Exception as exc:
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'比較レコードを確認できません: {exc}',
        )
    count = int(row[0]) if row else 0
    return AutoCheckResult(
        verdict='pass' if count >= 1 else 'fail',
        detail_ja=(
            f'A/B比較レコード: {count}件。'
            if count >= 1
            else 'A/B比較レコードが見つかりません。'
        ),
        evidence={'comparison_set_count': count},
    )


def check_gate_run_completed(ctx: CheckContext, arg: str) -> AutoCheckResult:
    """Cross-gate reference: a *passed* run of ``arg`` exists in this DB."""
    if ctx.repository is None:
        return AutoCheckResult(
            verdict='unavailable', detail_ja='リポジトリ参照がありません。'
        )
    prior = ctx.repository.latest_passed_run(arg)
    if prior is None:
        return AutoCheckResult(
            verdict='fail',
            detail_ja=f'ゲート「{arg}」の合格済み実行がまだありません。',
            evidence={'required_gate': arg},
        )
    return AutoCheckResult(
        verdict='pass',
        detail_ja=f'ゲート「{arg}」の合格済み実行を確認しました。',
        evidence={
            'required_gate': arg,
            'run_id': prior.run_id,
            'run_sha256': prior.run_sha256,
            'finished_at_utc': prior.finished_at_utc,
        },
    )


# ----------------------------------------------------------------------
# repo-script subprocess checks


def _run_repo_script(
    ctx: CheckContext,
    script_name: str,
    extra_args: list[str],
    *,
    timeout_s: int,
) -> AutoCheckResult:
    script = _repo_root() / 'scripts' / script_name
    if not script.is_file():
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'スクリプトが見つかりません: {script_name}',
        )
    try:
        proc = ctx.subprocess_runner(
            [sys.executable, str(script), *extra_args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_s,
            cwd=str(_repo_root()),
        )
    except subprocess.TimeoutExpired:
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'{script_name}がタイムアウトしました。',
        )
    tail = (proc.stdout or '').strip().splitlines()[-12:]
    stderr_tail = (proc.stderr or '').strip().splitlines()[-6:]
    return AutoCheckResult(
        verdict='pass' if proc.returncode == 0 else 'fail',
        detail_ja=(
            f'{script_name}: 成功'
            if proc.returncode == 0
            else f'{script_name}: 失敗 (exit={proc.returncode})'
        ),
        evidence={
            'exit_code': proc.returncode,
            'stdout_tail': tail,
            'stderr_tail': stderr_tail,
        },
    )


def check_dependency_lock(ctx: CheckContext, arg: str) -> AutoCheckResult:
    return _run_repo_script(
        ctx, 'check_dependency_lock.py', [], timeout_s=120
    )


def check_golden_path_preflight(ctx: CheckContext, arg: str) -> AutoCheckResult:
    work_dir = Path(
        tempfile.mkdtemp(prefix='htdt-acceptance-golden-')
    )
    trace_path = work_dir / 'golden-path-trace.json'
    try:
        result = _run_repo_script(
            ctx,
            'golden_path_preflight.py',
            [
                '--work-dir',
                str(work_dir),
                '--trace',
                str(trace_path),
                '--clean-work-dir-on-success',
            ],
            timeout_s=1800,
        )
        evidence = dict(result.evidence)
        if trace_path.is_file():
            import hashlib

            payload = trace_path.read_bytes()
            evidence['trace_sha256'] = hashlib.sha256(payload).hexdigest()
            evidence['trace_size_bytes'] = len(payload)
        return AutoCheckResult(
            verdict=result.verdict,
            detail_ja=result.detail_ja,
            evidence=evidence,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def check_o60r_audit(ctx: CheckContext, arg: str) -> AutoCheckResult:
    campaign_id = ctx.input_value.strip()
    if not campaign_id:
        return AutoCheckResult(
            verdict='fail',
            detail_ja='監査対象のキャンペーンIDを入力してください。',
        )
    return _run_repo_script(
        ctx,
        'audit_o60_owned_room.py',
        [
            '--data-dir',
            str(ctx.data_dir),
            '--campaign-id',
            campaign_id,
        ],
        timeout_s=300,
    )


# ----------------------------------------------------------------------
# registry

CheckFn = Callable[[CheckContext, str], AutoCheckResult]

AUTO_CHECKS: dict[str, CheckFn] = {
    'env_snapshot': check_env_snapshot,
    'rew_engine_probe': check_rew_engine_probe,
    'rew_audio_ready': check_rew_audio_ready,
    'rew_input_ready': check_rew_input_ready,
    'rew_calibration_selected': check_rew_calibration_selected,
    'rew_output_mapping': check_rew_output_mapping,
    'rew_measurement_count': check_rew_measurement_count,
    'persistence_probe': check_persistence_probe,
    'backup_restore_roundtrip': check_backup_restore_roundtrip,
    'campaign_registered': check_campaign_registered,
    'comparison_present': check_comparison_present,
    'gate_run_completed': check_gate_run_completed,
    'dependency_lock_check': check_dependency_lock,
    'golden_path_preflight': check_golden_path_preflight,
    'o60r_audit': check_o60r_audit,
}


def run_auto_check(check_spec: str, ctx: CheckContext) -> AutoCheckResult:
    """Execute ``name[:arg]``. Unknown check ids fail closed."""
    check_id, _, arg = check_spec.partition(':')
    fn = AUTO_CHECKS.get(check_id)
    if fn is None:
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'未登録の自動チェックです: {check_id}',
        )
    try:
        return fn(ctx, arg)
    except Exception as exc:  # fail-closed wrapper
        return AutoCheckResult(
            verdict='unavailable',
            detail_ja=f'チェック実行中にエラー: {type(exc).__name__}: {exc}',
            evidence={'error': f'{type(exc).__name__}: {exc}'},
        )
