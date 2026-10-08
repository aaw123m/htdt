"""#888 — headless ``htdt`` CLI + automation API contract tests.

Covers: per-verb exit-code lattice (fail-closed), JSON envelope schema
stability, fake-backend end-to-end sweep→verify, dry-run non-mutation,
seal/id integrity of ``CadHeadlessRunRecord`` + repository round-trip
with tamper detection, and the Qt-free import guard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from htdt.cad_headless_cli import (
    CadHeadlessRunRecord,
    HeadlessSweepSpec,
    OUTCOME_EXIT_CODES,
    build_run_record,
    spec_sha256,
)
from htdt.cad_headless_cli_repository import CadHeadlessRunRepository
from htdt.cad_calibration_deployment_repository import (
    DeploymentIntegrityError,
)
from htdt.cad_schema import NATIVE_SCHEMA_VERSION
from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES
from htdt.project_library_repository import SceneRepository
from htdt.headless_cli import main as cli_main


DOC = 'doc-888'
NOW = '2026-10-08T00:00:00Z'

_STIMULUS = {
    'start_frequency_hz': 100.0,
    'end_frequency_hz': 8000.0,
    'duration_s': 0.05,
    'level_dbfs': -12.0,
    'sample_rate_hz': 48000,
    'pre_roll_s': 0.01,
    'post_roll_s': 0.01,
    'fade_in_s': 0.002,
    'fade_out_s': 0.002,
    'repetitions': 1,
    'repetition_gap_s': 0.01,
}

_ROUTING = {
    'playback_device_id': 'fake-duplex-0',
    'playback_channel': 0,
    'capture_device_id': 'fake-duplex-0',
    'capture_channel': 0,
}


def _write(path: Path, payload: dict) -> str:
    path.write_text(
        json.dumps(payload, indent=2), encoding='utf-8')
    return str(path)


def _run(capsys, *argv):
    code = cli_main([str(a) for a in argv])
    out = capsys.readouterr().out.strip().splitlines()
    assert out, f'no stdout for {argv}'
    env = json.loads(out[-1])
    return code, env


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / 'data'


def _sweep_spec(tmp_path: Path, **overrides) -> str:
    spec = {
        'document_id': DOC,
        'backend': 'fake',
        'stimulus': dict(_STIMULUS),
        'routing': dict(_ROUTING),
    }
    spec.update(overrides)
    return _write(tmp_path / 'sweep.json', spec)


def _cvplan_spec(tmp_path: Path) -> str:
    spec = {
        'document_id': DOC,
        'chains': [
            {'logical_channel': 'fl',
             'channel_class': 'bed_channel',
             'expected_speaker_entity_ids': ['spk-fl']},
            {'logical_channel': 'fr',
             'channel_class': 'bed_channel',
             'expected_speaker_entity_ids': ['spk-fr']},
        ],
        'routings': {
            'fl': dict(_ROUTING),
            'fr': {'playback_device_id': 'fake-duplex-0',
                   'playback_channel': 1,
                   'capture_device_id': 'fake-duplex-0',
                   'capture_channel': 1},
        },
        'stimulus_duration_s': 0.05,
        'repetitions': 2,
        'reference_channel': 'fl',
    }
    return _write(tmp_path / 'cvplan.json', spec)


# ---------- envelope contract ----------

ENVELOPE_KEYS = {
    'format', 'exit_code', 'verb', 'outcome', 'verdict', 'reason',
    'records', 'run_record_id', 'run_sha256', 'data',
}


def test_envelope_schema_stable_success_and_failure(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    code, env = _run(capsys, 'status', '--json',
                     '--data-dir', data_dir)
    assert code == 0
    assert set(env.keys()) == ENVELOPE_KEYS
    assert env['format'] == 'htdt-headless-result-1'
    assert env['outcome'] == 'succeeded'
    assert env['exit_code'] == 0
    assert env['verb'] == 'status'
    assert isinstance(env['records'], list)
    assert env['data']['tool_version']
    assert env['data']['env_fingerprint']

    code, env = _run(
        capsys, 'project', 'inspect', '--document-id', 'nope',
        '--json', '--data-dir', data_dir)
    assert code == 5
    assert set(env.keys()) == ENVELOPE_KEYS
    assert env['outcome'] == 'missing_evidence'


def test_exit_code_lattice_constant():
    assert OUTCOME_EXIT_CODES == {
        'succeeded': 0, 'dry_run': 0, 'failed': 2, 'blocked': 3,
        'unauthorized': 4, 'missing_evidence': 5, 'cancelled': 7,
    }


def test_usage_error_exit_1(tmp_path, capsys):
    code, env = _run(
        capsys, 'sweep', 'run', '--json', '--data-dir',
        _data_dir(tmp_path))
    assert code == 1
    assert env['exit_code'] == 1
    assert env['outcome'] == 'missing_evidence'


# ---------- project verbs ----------

def test_project_create_and_inspect(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    code, env = _run(
        capsys, 'project', 'create', '--name', 'Batch',
        '--description', 'x', '--document-id', DOC,
        '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['verb'] == 'project.create'
    assert env['verdict'] == 'created'
    assert env['run_record_id'].startswith('hrun-')
    kinds = {r['kind'] for r in env['records']}
    assert 'project' in kinds

    code, env = _run(
        capsys, 'project', 'inspect', '--document-id', DOC,
        '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['data']['project']['document_id'] == DOC


def test_project_create_dry_run_no_mutation(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    code, env = _run(
        capsys, 'project', 'create', '--name', 'Batch',
        '--document-id', DOC, '--dry-run', '--json',
        '--data-dir', data_dir)
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['run_record_id'] is None
    code, env = _run(
        capsys, 'project', 'inspect', '--document-id', DOC,
        '--json', '--data-dir', data_dir)
    assert code == 5  # nothing was persisted


# ---------- sweep.run lattice ----------

def test_sweep_run_requires_arm(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _sweep_spec(tmp_path)
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--json',
        '--data-dir', data_dir)
    assert code == 4
    assert env['outcome'] == 'unauthorized'
    assert env['verdict'] == 'arm_required'
    # refused attempts are still sealed evidence
    assert env['run_record_id'].startswith('hrun-')

    code, env = _run(
        capsys, 'records', 'export', '--kind', 'headless_run',
        '--id', env['run_record_id'], '--json',
        '--data-dir', data_dir)
    assert code == 0
    assert env['data']['payload']['verb'] == 'sweep.run'
    assert env['data']['payload']['outcome'] == 'unauthorized'


def test_sweep_run_fake_backend_end_to_end(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _sweep_spec(tmp_path)
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', data_dir)
    assert code == 0, env
    assert env['outcome'] == 'succeeded'
    assert env['verdict'] in ('valid', 'limited')
    kinds = {r['kind'] for r in env['records']}
    assert {'sweep_stimulus_definition', 'sweep_acquisition_run'} <= kinds
    assert env['run_record_id'].startswith('hrun-')

    # run record payload pins spec sha + tool identity
    code, env2 = _run(
        capsys, 'records', 'export', '--kind', 'headless_run',
        '--id', env['run_record_id'], '--json',
        '--data-dir', data_dir)
    payload = env2['data']['payload']
    parsed = HeadlessSweepSpec.model_validate(
        json.loads(Path(spec).read_text(encoding='utf-8')))
    assert payload['spec_sha256'] == spec_sha256(parsed)
    assert payload['tool_version']
    assert payload['argv_sha256']
    assert payload['env_fingerprint']
    assert payload['backend_is_simulated'] is True


def test_sweep_run_precheck_blocked_routing(tmp_path, capsys):
    spec = _sweep_spec(
        tmp_path,
        routing={
            'playback_device_id': 'no-such-device',
            'playback_channel': 0,
            'capture_device_id': 'fake-duplex-0',
            'capture_channel': 0,
        })
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 3
    assert env['outcome'] == 'blocked'


def test_sweep_run_device_loss_failed(tmp_path, capsys):
    spec = _sweep_spec(
        tmp_path,
        fake_scenario={'device_loss_at_frame': 32})
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 2
    assert env['outcome'] == 'failed'


def test_sweep_run_cancelled(tmp_path, capsys):
    # cancel is observed at a repetition boundary → needs 2+ reps
    stim = dict(_STIMULUS)
    stim['repetitions'] = 2
    spec = _sweep_spec(
        tmp_path, stimulus=stim,
        fake_scenario={'cancel_after_frames': 16})
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 7
    assert env['outcome'] == 'cancelled'


def test_sweep_run_dry_run_writes_nothing(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _sweep_spec(tmp_path)
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--dry-run', '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['verdict'] == 'precheck_ok'
    assert env['run_record_id'] is None
    # no sealed store was ever opened
    assert not (data_dir / 'cad-scenes.sqlite3').exists()


def test_sweep_run_bad_spec_missing_evidence(tmp_path, capsys):
    spec = _write(tmp_path / 'bad.json', {'document_id': DOC})
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 5
    assert env['outcome'] == 'missing_evidence'
    assert env['run_record_id'] is None


# ---------- channel-verify plan/run ----------

def test_channel_verify_plan_then_run(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _cvplan_spec(tmp_path)
    out_file = str(tmp_path / 'cvplan-out.json')
    code, env = _run(
        capsys, 'channel-verify', 'plan', '--spec', spec,
        '--out', out_file, '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['verdict'] == 'planned'
    plan_id = env['records'][0]['ref_id']
    assert plan_id.startswith('cvpl-')
    assert Path(out_file).exists()

    # simulated backend can at most produce ambiguous evidence
    code, env = _run(
        capsys, 'channel-verify', 'run', '--plan', out_file,
        '--arm', '--json', '--data-dir', data_dir)
    assert code == 3
    assert env['outcome'] == 'blocked'
    assert env['verdict'] == 'ambiguous'
    kinds = {r['kind'] for r in env['records']}
    assert 'channel_verification_verdict' in kinds
    assert 'channel_excitation_result' in kinds
    # the run verb seals its own run record under the plan's document
    assert env['run_record_id'].startswith('hrun-')


def test_channel_verify_run_requires_arm(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _cvplan_spec(tmp_path)
    code, env = _run(
        capsys, 'channel-verify', 'plan', '--spec', spec,
        '--json', '--data-dir', data_dir)
    plan_id = env['records'][0]['ref_id']
    code, env = _run(
        capsys, 'channel-verify', 'run', '--plan-id', plan_id,
        '--json', '--data-dir', data_dir)
    assert code == 4
    assert env['outcome'] == 'unauthorized'


def test_channel_verify_run_missing_plan(tmp_path, capsys):
    code, env = _run(
        capsys, 'channel-verify', 'run', '--plan-id', 'cvpl-nope',
        '--arm', '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 5
    assert env['outcome'] == 'missing_evidence'


# ---------- campaign plan/run ----------

def _campaign_spec(tmp_path: Path) -> str:
    spec = {
        'document_id': DOC,
        'campaign_ref': {'kind': 'campaign_declaration',
                         'ref_id': 'camp-1',
                         'ref_sha256': 'a' * 64},
        'stimulus_template': dict(_STIMULUS),
        'channel_bindings': [
            {'channel_entity_id': 'fl',
             'playback_device_id': 'fake-duplex-0',
             'playback_channel': 0,
             'capture_device_id': 'fake-duplex-0',
             'capture_channel': 0},
        ],
        'positions': [
            {'position_id': 'seat-1', 'roles': ['calibration'],
             'channel_entity_ids': ['fl']},
        ],
        'repetitions_per_entry': 1,
    }
    return _write(tmp_path / 'campaign.json', spec)


def test_campaign_plan_and_run(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _campaign_spec(tmp_path)
    out_file = str(tmp_path / 'campaign-plan.json')
    code, env = _run(
        capsys, 'campaign', 'plan', '--spec', spec,
        '--out', out_file, '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['verdict'] == 'planned'

    code, env = _run(
        capsys, 'campaign', 'run', '--plan', out_file,
        '--arm', '--auto-confirm-position', '--json',
        '--data-dir', data_dir)
    assert code in (0, 2), env
    assert env['outcome'] in ('succeeded', 'failed')
    assert env['run_record_id'].startswith('hrun-')


def test_campaign_run_blocked_or_unauthorized(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _campaign_spec(tmp_path)
    code, env = _run(
        capsys, 'campaign', 'plan', '--spec', spec,
        '--json', '--data-dir', data_dir)
    plan_id = env['records'][0]['ref_id']
    # no --arm → unauthorized before the runner even starts
    code, env = _run(
        capsys, 'campaign', 'run', '--plan-id', plan_id,
        '--json', '--data-dir', data_dir)
    assert code == 4
    assert env['outcome'] == 'unauthorized'


# ---------- calibration ----------

def _calibration_spec(tmp_path: Path) -> str:
    spec = {
        'document_id': DOC,
        'backend': 'fake',
        'lane': 'interface_loopback',
        'io_path': {
            'output_device': 'fake-duplex-0',
            'output_channel': '0',
            'input_device': 'fake-duplex-0',
            'input_channel': '0',
        },
        'routing': {**_ROUTING, 'loopback_input_channel': 1},
        'stimulus_spec': dict(_STIMULUS),
        'max_steps': 8,
    }
    return _write(tmp_path / 'cal.json', spec)


def test_calibration_blocked_at_hardware_gate(tmp_path, capsys):
    spec = _calibration_spec(tmp_path)
    code, env = _run(
        capsys, 'calibration', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 3
    assert env['outcome'] == 'blocked'
    assert env['verdict'].startswith('await_')


def test_calibration_confirm_hardware_reaches_terminal(tmp_path, capsys):
    spec = _calibration_spec(tmp_path)
    code, env = _run(
        capsys, 'calibration', 'run', '--spec', spec, '--arm',
        '--confirm-hardware', '--json',
        '--data-dir', _data_dir(tmp_path))
    assert env['outcome'] in ('succeeded', 'failed', 'blocked')
    assert code == OUTCOME_EXIT_CODES[env['outcome']]


def test_calibration_dry_run(tmp_path, capsys):
    spec = _calibration_spec(tmp_path)
    code, env = _run(
        capsys, 'calibration', 'run', '--spec', spec, '--arm',
        '--dry-run', '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['run_record_id'] is None


# ---------- diagnostic ----------

def _diagnostic_spec(tmp_path: Path) -> str:
    return _write(tmp_path / 'diag.json', {
        'document_id': DOC,
        'backend': 'fake',
        'fault_tree_id': 'channel_missing_output',
        'symptom_summary': 'no output on fl',
        'routings': {'fl': dict(_ROUTING)},
        'reference_channel': 'fl',
        'max_steps': 8,
    })


def test_diagnostic_run_bounded(tmp_path, capsys):
    spec = _diagnostic_spec(tmp_path)
    code, env = _run(
        capsys, 'diagnose', 'run', '--spec', spec,
        '--json', '--data-dir', _data_dir(tmp_path))
    assert env['outcome'] in OUTCOME_EXIT_CODES
    assert code == OUTCOME_EXIT_CODES[env['outcome']]
    assert env['run_record_id'].startswith('hrun-')


def test_diagnostic_dry_run(tmp_path, capsys):
    spec = _diagnostic_spec(tmp_path)
    code, env = _run(
        capsys, 'diagnose', 'run', '--spec', spec,
        '--dry-run', '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['run_record_id'] is None


# ---------- records verbs ----------

def test_records_list_and_export_roundtrip(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    spec = _sweep_spec(tmp_path)
    code, env = _run(
        capsys, 'sweep', 'run', '--spec', spec, '--arm',
        '--json', '--data-dir', data_dir)
    run_id = env['run_record_id']

    code, env = _run(
        capsys, 'records', 'list', '--kind', 'headless_run',
        '--json', '--data-dir', data_dir)
    assert code == 0
    assert env['data']['count'] >= 1
    ids = [r['id'] for r in env['data']['records']]
    assert run_id in ids

    code, env = _run(
        capsys, 'records', 'export', '--kind', 'headless_run',
        '--id', run_id, '--out',
        str(tmp_path / 'exported.json'),
        '--json', '--data-dir', data_dir)
    assert code == 0
    exported = json.loads(
        (tmp_path / 'exported.json').read_text(encoding='utf-8'))
    assert exported['run_record_id'] == run_id

    code, env = _run(
        capsys, 'records', 'export', '--kind', 'sweep_run',
        '--id', 'swrun-nope', '--json', '--data-dir', data_dir)
    assert code == 5
    assert env['outcome'] == 'missing_evidence'


# ---------- sealed record model + repository ----------

def _record(**overrides) -> CadHeadlessRunRecord:
    kwargs = {
        'document_id': DOC,
        'verb': 'sweep.run',
        'outcome': 'succeeded',
        'spec': None,
        'spec_json': '{"document_id":"doc-888"}',
        'spec_sha': 'a' * 64,
        'tool_version': '0.2.0.dev0+gabc',
        'tool_commit_sha': 'abc123',
        'tool_commit_dirty': False,
        'python_version': '3.12.10',
        'platform': 'Windows',
        'env_fingerprint': 'f' * 40,
        'argv_sha256': 'b' * 64,
        'backend_id': 'fake-audio-io',
        'backend_is_simulated': True,
        'record_refs': (),
        'dry_run': False,
        'cancelled': False,
        'started_at_utc': NOW,
        'finished_at_utc': NOW,
        'elapsed_ms': 12,
    }
    kwargs.update(overrides)
    return build_run_record(**kwargs)


def test_run_record_seal_identity():
    record = _record()
    assert record.run_record_id == 'hrun-' + record.run_sha256[:24]
    assert len(record.run_sha256) == 64
    # identity is deterministic on identical fields
    assert _record().run_sha256 == record.run_sha256
    # outcome changes alter the seal
    assert _record(outcome='failed').run_sha256 != record.run_sha256


def test_run_record_rejects_bad_timestamp():
    with pytest.raises(Exception):
        _record(started_at_utc='not-a-timestamp')


def test_repository_roundtrip_and_tamper(tmp_path):
    path = tmp_path / 'cad-scenes.sqlite3'
    repo = CadHeadlessRunRepository(SceneRepository(path))
    record = _record()
    repo.save_run(record)
    loaded = repo.get_run(record.run_record_id)
    assert loaded is not None
    assert loaded.run_sha256 == record.run_sha256
    assert loaded.spec_json == record.spec_json
    assert loaded.document_id == DOC
    assert repo.get_run('hrun-nope') is None
    assert len(repo.list_runs(document_id=DOC)) == 1
    assert repo.list_runs(document_id='other') == []
    assert len(repo.list_runs(verb='sweep.run')) == 1
    assert repo.list_runs(verb='deploy.run') == []

    # tamper detection: mutate a column so it disagrees with the
    # sealed payload → read-time column-vs-payload check trips
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute(
        "UPDATE cad_headless_run_records SET verb='tampered' "
        "WHERE run_record_id=?", (record.run_record_id,))
    con.commit()
    con.close()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_run(record.run_record_id)


def test_schema_registered():
    assert NATIVE_SCHEMA_VERSION >= 108
    assert 'cad_headless_run_records' in NATIVE_SCHEMA_TABLES


# ---------- Qt-free import guard ----------

def test_headless_cli_import_is_qt_free(tmp_path):
    src = str(Path(__file__).resolve().parents[1] / 'src')
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    code = (
        'import sys\n'
        f'sys.path.insert(0, {src!r})\n'
        'import htdt.headless_cli\n'
        'import htdt.cad_headless_cli\n'
        'import htdt.cad_headless_cli_repository\n'
        'qt = [m for m in sys.modules\n'
        '      if m.split(".")[0] in ("PySide6", "PySide2",\n'
        '         "PyQt5", "PyQt6", "qtpy")]\n'
        'assert not qt, qt\n'
        'sys.exit(0)\n')
    probe = tmp_path / 'qt_probe.py'
    probe.write_text(code, encoding='utf-8')
    result = subprocess.run(
        [sys.executable, str(probe)],
        env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr


# ---------- spec hashing determinism ----------

def test_spec_sha_deterministic_and_tamper_sensitive(tmp_path):
    spec_dict = json.loads(
        Path(_sweep_spec(tmp_path)).read_text(encoding='utf-8'))
    parsed = HeadlessSweepSpec.model_validate(spec_dict)
    sha1 = spec_sha256(parsed)
    parsed_again = HeadlessSweepSpec.model_validate(spec_dict)
    assert spec_sha256(parsed_again) == sha1
    spec_dict['routing']['capture_channel'] = 2
    tampered = HeadlessSweepSpec.model_validate(spec_dict)
    assert spec_sha256(tampered) != sha1


# ---------- deployment ----------

def _deploy_spec(tmp_path: Path) -> str:
    """Minimal avr-lan deployment spec with a real sealed export payload."""
    from hashlib import sha256
    from htdt.cad_calibration import (
        CadCalibrationChannel,
        CadCalibrationPlan,
        CadDeviceCapabilityConstraints,
        build_generic_biquad_export,
    )
    channel = CadCalibrationChannel(
        channel_id='fl', role_id='FL',
        source_entity_id='spk-fl', physical_output_id='out-fl',
        sample_rate_hz=48000, gain_db=1.5, delay_s=0.0,
        polarity='normal', crossovers=(), peq=(),
        routing=('out-fl',))
    payload = {
        'plan_id': 'plan-888', 'plan_version': '1',
        'created_at_utc': NOW, 'source_kind': 'provided_fixture',
        'document_id': DOC, 'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000, 'channels': (channel,),
        'target_curve': None, 'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': CadDeviceCapabilityConstraints(
            capability_id='avr-1', capability_version='1',
            supported_sample_rates_hz=(48000,),
            supported_filter_types=('peaking',),
            channel_gain_resolution_db=0.5),
        'support_state': 'SUPPORTED', 'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    sem = sha256(json.dumps(
        provisional.semantic_payload(), ensure_ascii=False,
        sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    plan = CadCalibrationPlan(
        **{**payload, 'plan_semantic_sha256': sem})
    export = build_generic_biquad_export(
        plan=plan, created_at_utc=NOW)
    spec = {
        'document_id': DOC,
        'adapter': {'kind': 'avr-lan', 'simulated': True,
                    'initial_gains': {}},
        'binding': {
            'device_family': 'avr-denon-marantz-telnet',
            'device_model': 'AVR-X3800H',
            'device_serial': 'localhost:23',
            'firmware_version': '1.4.0',
            'routing': [['fl', 'out-fl']]},
        'target_ref': 'avr-target-1',
        'export': export.model_dump(mode='json'),
    }
    return _write(tmp_path / 'deploy.json', spec)


def test_deploy_run_lattice(tmp_path, capsys):
    spec = _deploy_spec(tmp_path)

    # preview only → exits after preview, 0
    code, env = _run(
        capsys, 'deploy', 'run', '--spec', spec,
        '--preview-only', '--json',
        '--data-dir', _data_dir(tmp_path) / 'a')
    assert code == 0
    assert env['verdict'] == 'previewed'

    # no --authorize-apply → unauthorized, 4
    code, env = _run(
        capsys, 'deploy', 'run', '--spec', spec,
        '--json', '--data-dir', _data_dir(tmp_path) / 'b')
    assert code == 4
    assert env['outcome'] == 'unauthorized'
    assert env['verdict'] == 'authorize_apply_required'

    # authorized apply+rollback → apply + verify_readback → 0
    code, env = _run(
        capsys, 'deploy', 'run', '--spec', spec,
        '--authorize-apply', 'op-1',
        '--authorize-rollback', 'op-1',
        '--json', '--data-dir', _data_dir(tmp_path) / 'c')
    assert code == 0
    assert env['outcome'] == 'succeeded'
    assert env['verdict'] == 'readback_matched'
    kinds = {r['kind'] for r in env['records']}
    assert 'deployment_pipeline_record' in kinds
    assert env['run_record_id'].startswith('hrun-')


def test_deploy_dry_run(tmp_path, capsys):
    spec = _deploy_spec(tmp_path)
    code, env = _run(
        capsys, 'deploy', 'run', '--spec', spec,
        '--authorize-apply', 'op-1', '--dry-run',
        '--json', '--data-dir', _data_dir(tmp_path))
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['run_record_id'] is None
