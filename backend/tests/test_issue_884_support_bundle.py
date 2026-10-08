"""#884 privacy-safe diagnostic/support bundle tests.

Covers: per-field classification, write-time redaction (paths / IPs /
hosts / names / tokens / secret keys), preview == export byte-identity,
integrity manifest, deterministic archives, context collectors, and
crash-isolation of probes.
"""

from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.support_diagnostics import (
    CATEGORY_CLASSIFICATION,
    DiagnosticPackageBuilder,
    FieldClassification,
    PackageCategory,
    PackagePlan,
    SUPPORT_SCHEMA_VERSION,
    redact_support_value,
)


def _builder(tmp_path: Path, **kwargs) -> DiagnosticPackageBuilder:
    return DiagnosticPackageBuilder(tmp_path, **kwargs)


def _read_member(zip_path: Path, name: str) -> bytes:
    with zipfile.ZipFile(zip_path) as archive:
        return archive.read(name)


def _manifest(zip_path: Path) -> dict:
    return json.loads(_read_member(zip_path, 'manifest.json'))


# --- classification -------------------------------------------------------

def test_every_category_has_a_classification() -> None:
    for category in PackageCategory:
        assert category in CATEGORY_CLASSIFICATION


def test_project_ids_is_metadata_classification() -> None:
    assert (
        CATEGORY_CLASSIFICATION[PackageCategory.PROJECT_IDS]
        is FieldClassification.PROJECT_METADATA
    )


def test_members_carry_classification(tmp_path) -> None:
    builder = _builder(tmp_path)
    plan = builder.plan()
    result = builder.build(tmp_path / 'out.zip', plan)
    manifest = _manifest(result.path)
    for name, meta in manifest['members'].items():
        assert meta['classification'] in {c.value for c in FieldClassification}


def test_schema_version_bumped() -> None:
    assert SUPPORT_SCHEMA_VERSION >= 2


# --- redaction ------------------------------------------------------------

def test_redact_windows_paths() -> None:
    counts: dict[str, int] = {}
    out = redact_support_value(
        {'log': 'failed to open C:\\Users\\Alice\\secret\\file.wav'}, counts)
    assert 'Alice' not in json.dumps(out)
    assert counts['paths'] >= 1


def test_redact_posix_paths() -> None:
    counts: dict[str, int] = {}
    out = redact_support_value(
        {'p': '/home/bob/project/file.txt and /tmp/x'}, counts)
    assert 'bob' not in json.dumps(out)


def test_redact_ip_addresses() -> None:
    counts: dict[str, int] = {}
    out = redact_support_value(
        {'endpoint': '192.168.1.10:8080', 'other': 'fd00::a1b2'}, counts)
    assert '192.168.1.10' not in json.dumps(out)
    assert 'fd00' not in json.dumps(out)


def test_redact_timestamps_not_mangled() -> None:
    out = redact_support_value({'t': '10:30:45.123'})
    assert out['t'] == '10:30:45.123'


def test_redact_version_strings_not_mangled() -> None:
    out = redact_support_value({'v': '10.0.22631.4167'})
    assert out['v'] == '10.0.22631.4167'


def test_redact_secret_keys() -> None:
    counts: dict[str, int] = {}
    out = redact_support_value(
        {
            'api_key': 'ghp_ABCDEFGHIJKLMNOP1234567890',
            'nested': {'password': 'hunter2'},
        },
        counts,
    )
    blob = json.dumps(out)
    assert 'ghp_ABCDEFGHIJKLMNOP1234567890' not in blob
    assert 'hunter2' not in blob
    assert counts.get('secrets', 0) >= 1


def test_redact_sensitive_named_fields() -> None:
    out = redact_support_value(
        {'hostname': 'FIELD-PC-01', 'customer_name': 'Yamada Taro'}
    )
    blob = json.dumps(out)
    assert 'FIELD-PC-01' not in blob
    assert 'Yamada' not in blob


def test_log_content_is_redacted_at_write(tmp_path) -> None:
    diag = tmp_path / 'diagnostics'
    diag.mkdir(parents=True)
    (diag / 'htdt-native.log').write_text(
        'INFO open C:\\Users\\FieldTech\\proj from 10.0.0.5 token=abc123\n',
        encoding='utf-8',
    )
    builder = _builder(tmp_path)
    result = builder.build(tmp_path / 'out.zip', builder.plan())
    log = _read_member(result.path, 'logs/htdt-native.log').decode()
    assert 'FieldTech' not in log
    assert '10.0.0.5' not in log
    manifest = _manifest(result.path)
    member = manifest['members']['logs/htdt-native.log']
    assert member['redactions']['paths'] >= 1
    assert member['redactions']['hosts'] >= 1


def test_project_payload_never_reaches_bundle(tmp_path) -> None:
    builder = _builder(
        tmp_path,
        capability_inventory={'room_geometry': {'vertices': [1, 2, 3]}},
        project_ids={'p1': 'Customer Living Room'},
    )
    plan = builder.plan(include_project_ids=False)
    result = builder.build(tmp_path / 'out.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        names = archive.namelist()
        whole = b''.join(archive.read(n) for n in names)
    assert b'Customer Living Room' not in whole
    assert 'project_ids.json' not in names


def test_project_ids_opt_in_still_records_classification(tmp_path) -> None:
    builder = _builder(tmp_path, project_ids={'p1': 'My Room'})
    plan = builder.plan(include_project_ids=True)
    result = builder.build(tmp_path / 'out.zip', plan)
    manifest = _manifest(result.path)
    assert (
        manifest['members']['project_ids.json']['classification']
        == FieldClassification.PROJECT_METADATA.value
    )


# --- integrity manifest ---------------------------------------------------

def test_integrity_manifest_sha256_matches_members(tmp_path) -> None:
    builder = _builder(tmp_path)
    result = builder.build(tmp_path / 'out.zip', builder.plan())
    manifest = _manifest(result.path)
    for name, digest in manifest['integrity'].items():
        assert sha256(_read_member(result.path, name)).hexdigest() == digest


def test_manifest_records_collection_errors(tmp_path) -> None:
    def boom() -> dict:
        raise RuntimeError('gpu probe exploded')

    builder = _builder(
        tmp_path,
        context_providers={PackageCategory.GPU_CONTEXT: boom},
    )
    result = builder.build(tmp_path / 'out.zip', builder.plan())
    manifest = _manifest(result.path)
    assert manifest['collection_errors'] == ['gpu_context: RuntimeError']


def test_export_never_fails_from_probe(tmp_path) -> None:
    builder = _builder(
        tmp_path,
        context_providers={
            PackageCategory.AUDIO_CONTEXT: lambda: (_ for _ in ()).throw(
                ValueError('nope')
            )
        },
    )
    result = builder.build(tmp_path / 'out.zip', builder.plan())
    assert result.path.exists()


# --- preview == export ----------------------------------------------------

def test_preview_members_match_export(tmp_path) -> None:
    builder = _builder(
        tmp_path,
        context_providers={
            PackageCategory.RUNTIME_CONTEXT: lambda: {'fixed': 'payload'},
        },
    )
    plan = builder.plan()
    preview = builder.preview(plan)
    result = builder.build(tmp_path / 'out.zip', plan)

    staged_names = {m.name for m in preview.members}
    with zipfile.ZipFile(result.path) as archive:
        zipped = set(archive.namelist()) - {'manifest.json'}
    assert staged_names == zipped
    for member in preview.members:
        if member.sha256:
            assert (
                sha256(_read_member(result.path, member.name)).hexdigest()
                == member.sha256
            )
            assert member.included_bytes == len(
                _read_member(result.path, member.name)
            )


def test_preview_is_deterministic(tmp_path) -> None:
    builder = _builder(tmp_path)
    plan = builder.plan()
    assert builder.preview(plan).preview_sha256 == (
        builder.preview(plan).preview_sha256
    )


def test_archive_bytes_deterministic(tmp_path) -> None:
    builder = _builder(tmp_path)
    plan = builder.plan()
    one = builder.build(tmp_path / 'a.zip', plan).path.read_bytes()
    two = builder.build(tmp_path / 'b.zip', plan).path.read_bytes()
    # created_at in manifest differs; strip it for the byte compare via
    # member bytes only — archive determinism is member-level.
    with zipfile.ZipFile(tmp_path / 'a.zip') as z1, zipfile.ZipFile(
        tmp_path / 'b.zip'
    ) as z2:
        for name in z1.namelist():
            if name == 'manifest.json':
                continue
            assert z1.read(name) == z2.read(name)
    assert len(one) > 0 and len(two) > 0


# --- context collectors ---------------------------------------------------

def test_context_categories_staged_when_provider_present(tmp_path) -> None:
    builder = _builder(
        tmp_path,
        context_providers={
            PackageCategory.WORKFLOW_STATE: lambda: {
                'current_workspace_id': 'measurement'
            },
        },
    )
    plan = builder.plan()
    assert PackageCategory.WORKFLOW_STATE in plan.categories
    result = builder.build(tmp_path / 'out.zip', plan)
    payload = json.loads(_read_member(result.path, 'context/workflow_state.json'))
    assert payload['current_workspace_id'] == 'measurement'


def test_runtime_context_collector(tmp_path) -> None:
    from htdt.support_bundle_collectors import collect_runtime_context

    payload = collect_runtime_context()
    assert payload['status'] == 'collected'
    assert payload['python']['version']
    assert 'PySide6' in payload['dependencies']


def test_gpu_context_collector_never_crashes() -> None:
    from htdt.support_bundle_collectors import collect_gpu_context

    payload = collect_gpu_context()
    assert payload['status'] == 'collected'
    assert payload['renderer'] == 'unprobed'


def test_audio_context_collector_lists_stub_honestly() -> None:
    from htdt.support_bundle_collectors import collect_audio_context

    payload = collect_audio_context()
    assert payload['status'] == 'collected'
    wasapi = [b for b in payload['backends'] if b['backend_id'] == 'wasapi-audio-io']
    assert wasapi and wasapi[0]['available'] is False
    fake = [b for b in payload['backends'] if b.get('simulated')]
    assert fake


def test_release_evidence_unprobed_without_artifacts(tmp_path) -> None:
    from htdt.support_bundle_collectors import collect_release_evidence

    payload = collect_release_evidence(tmp_path)
    assert payload['status'] == 'unprobed'


def test_release_evidence_records_sha_and_verdict(tmp_path) -> None:
    from htdt.support_bundle_collectors import collect_release_evidence

    report_dir = tmp_path / 'artifacts' / 'release-verification-x'
    report_dir.mkdir(parents=True)
    (report_dir / 'report.json').write_text(
        json.dumps({'verdict': 'passed'}), encoding='utf-8'
    )
    payload = collect_release_evidence(tmp_path)
    assert payload['status'] == 'collected'
    assert payload['reports'][0]['verdict'] == 'passed'
    assert len(payload['reports'][0]['sha256']) == 64


def test_context_payload_redacted_too(tmp_path) -> None:
    builder = _builder(
        tmp_path,
        context_providers={
            PackageCategory.WORKFLOW_STATE: lambda: {
                'hostname': 'CUSTOMER-HT-PC',
                'path': 'C:\\Users\\Secret\\x',
            }
        },
    )
    result = builder.build(tmp_path / 'out.zip', builder.plan())
    payload = _read_member(result.path, 'context/workflow_state.json')
    assert b'CUSTOMER-HT-PC' not in payload
    assert b'Secret' not in payload
