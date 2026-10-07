"""REV65 pipeline-slice regression tests.

One test per confirmed defect fixed in this review round
(see docs/reviews/rev65-pipeline.md). Each test fails on the pre-fix
code and passes on the fixed code. Everything is offline and local.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # register before exec: dataclass field introspection reads sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


signature = _load(
    'release_signature', ROOT / 'scripts' / 'release_signature.py'
)
ux160 = _load(
    'ux160_acceptance', ROOT / 'scripts' / 'ux160_acceptance.py'
)
fetch = _load(
    'fetch_external_corpus',
    ROOT / 'scripts' / 'fetch_external_corpus.py',
)
commit_v = _load(
    'commit_manifest_verification',
    ROOT / 'scripts' / 'commit_manifest_verification.py',
)
lifecycle = _load(
    'issue_lifecycle', ROOT / 'scripts' / 'issue_lifecycle.py'
)

WORKFLOW = ROOT / '.github' / 'workflows' / 'build-windows-artifacts.yml'
BUILD_NATIVE_PS1 = ROOT / 'scripts' / 'build-native.ps1'


# --- D1: release_signature.py dead non-signed merge path -----------------


def _signature_report(tmp_path: Path, **over) -> Path:
    base = {
        'file': 'HTDT-setup.exe',
        'status': 'unsigned',
        'pre_sign_sha256': 'a' * 64,
        'signed_sha256': '',
    }
    base.update(over)
    path = tmp_path / 'signature-report.json'
    path.write_text(json.dumps(base), encoding='utf-8')
    return path


def test_unsigned_signature_report_loads_and_merges(tmp_path: Path) -> None:
    """sign-release.ps1 emits signed_sha256='' for unsigned /
    signing_failed / unverifiable reports; the unconditional non-empty
    check rejected them in load_signature_report, so a non-signed
    publisher_signature status could never be merged."""
    report = signature.load_signature_report(_signature_report(tmp_path))
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(
        json.dumps({'schema': signature.MANIFEST_SCHEMA}),
        encoding='utf-8',
    )
    merged = signature.merge_signature_status(manifest_path, report)
    assert merged['publisher_signature']['status'] == 'unsigned'
    assert merged['publisher_signature']['signed_sha256'] is None


def test_signed_verified_report_requires_signed_sha256(
    tmp_path: Path,
) -> None:
    with pytest.raises(signature.SignatureMergeError):
        signature.load_signature_report(
            _signature_report(
                tmp_path, status='signed_verified', signed_sha256='  '
            )
        )


def test_non_signed_report_must_not_claim_digest(tmp_path: Path) -> None:
    with pytest.raises(signature.SignatureMergeError):
        signature.load_signature_report(
            _signature_report(
                tmp_path,
                status='signing_failed',
                signed_sha256='b' * 64,
            )
        )


# --- D2: ux160_acceptance.py uncaught subprocess timeout -----------------


def test_ux160_cell_timeout_is_recorded_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell subprocess hitting the 600 s timeout must land as a BLOCKED
    cell; an uncaught TimeoutExpired killed the whole matrix run before
    matrix.json / the report were written."""

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=['driver'], timeout=600)

    monkeypatch.setattr(
        ux160,
        'subprocess',
        SimpleNamespace(
            run=_raise_timeout,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    cell = ux160.run_cell(
        Path('python.exe'),
        '1.0',
        'seeded',
        tmp_path / 'data-seeded',
        tmp_path / 'out',
        tmp_path / 'cell.log',
    )
    assert cell['status'] == 'blocked'
    assert cell['returncode'] is None
    assert cell['detail'] == 'timeout'
    assert ux160.summarize_cell(cell) == {
        'status': 'BLOCKED',
        'detail': 'timeout',
    }


# --- D3: workflow artifact names collide across re-runs ------------------


def test_uploaded_artifact_names_unique_per_attempt() -> None:
    """upload-artifact v4+ artifact names are immutable — a re-run of the
    same run_number (same commit, same DISPLAY_VERSION) must not collide
    on the previous attempt's name."""
    text = WORKFLOW.read_text(encoding='utf-8')
    blocks = text.split('uses: actions/upload-artifact')
    assert len(blocks) > 1, 'no upload-artifact steps found'
    for block in blocks[1:]:
        match = re.search(r'^\s+name:\s*(.+?)\s*$', block, flags=re.M)
        assert match, 'upload-artifact step has no artifact name'
        assert 'github.run_attempt' in match.group(1), match.group(1)


# --- D4: fetch_external_corpus.py empty-plan pass + unbounded fetch ------


def test_unknown_dataset_id_fails_closed(tmp_path: Path) -> None:
    """An unknown --dataset id must not collapse into an empty plan that
    exits 0 having fetched and verified nothing."""
    with pytest.raises(SystemExit) as excinfo:
        fetch.run(
            [
                '--target-dir',
                str(tmp_path),
                '--dataset',
                'no-such-admission-id',
            ]
        )
    assert excinfo.value.code == 2


def test_fetch_uses_bounded_socket_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """urlopen without a timeout is an unbounded blocking read — an
    unattended fetch can hang forever on a stalled connection."""
    calls: list[dict] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _size: int) -> bytes:
            return b''

    def _urlopen(url, **kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(fetch.urllib.request, 'urlopen', _urlopen)
    step = fetch.CorpusFetchStep(
        admission_id='test-dataset',
        file_name='payload.bin',
        uri='https://example.invalid/payload.bin',
        target_relpath='dataset/payload.bin',
        size_bytes=0,
        checksum_source='computed',
    )
    fetch._fetch(step, tmp_path / 'dataset' / 'payload.bin', 42.0)
    assert calls and calls[0].get('timeout') == 42.0


# --- D5: build-native.ps1 dirty-bit collapses on git failure -------------


def test_build_native_ps1_git_status_failure_is_fatal() -> None:
    """`git status` failing must not stamp dirty=$false into build
    identity — the real dirty state is unknown, never clean."""
    text = BUILD_NATIVE_PS1.read_text(encoding='utf-8')
    swallow = '($LASTEXITCODE -eq 0) -and [bool]$Status'
    assert swallow not in text, (
        'dirty bit still collapses to $false when git status fails'
    )
    status_call = text.index('status --porcelain')
    guard_window = text[status_call:status_call + 400]
    assert 'LASTEXITCODE' in guard_window
    assert 'throw' in guard_window


# --- D6: commit_manifest_verification.py unreachable exit 3 --------------


def test_commit_manifest_verification_commit_failure_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented exit code 3 (integrity/commit failure) must be
    reachable; a sealed-store write failure previously propagated as a
    bare traceback exit 1, indistinguishable from a crash."""
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text('issues: []\n', encoding='utf-8')
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'issues': []}), encoding='utf-8')

    class _Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('sealed store unavailable')

    monkeypatch.setattr(
        commit_v, 'CadManifestGateRepository', _Unavailable
    )
    code = commit_v.main(
        [
            '--scene',
            str(tmp_path / 'scene.htdtscene'),
            '--document-id',
            'doc-test',
            '--manifest',
            str(manifest),
            '--report',
            str(report),
        ]
    )
    assert code == 3


# --- D7: issue_lifecycle.py dead check_statuses wiring --------------------


def test_checks_red_bucket_via_verification_report(tmp_path: Path) -> None:
    """classify_issue's check_statuses parameter was never supplied, so
    automated check failures on a landed issue could never reach the
    implementation_present_checks_red bucket."""
    lifecycle_manifest = tmp_path / 'lifecycle.yaml'
    lifecycle_manifest.write_text(
        'version: 1\n'
        'entries:\n'
        '  - issue: 1\n'
        '    lifecycle: software_landed\n'
        '    landed:\n'
        '      prs: [5]\n',
        encoding='utf-8',
    )
    verification_manifest = tmp_path / 'verification.yaml'
    verification_manifest.write_text('issues: []\n', encoding='utf-8')
    verification_report = tmp_path / 'report.json'
    verification_report.write_text(
        json.dumps(
            {
                'issues': [
                    {
                        'issue': 1,
                        'checks': [{'id': 'c1', 'status': 'failed'}],
                    }
                ]
            }
        ),
        encoding='utf-8',
    )
    json_out = tmp_path / 'out.json'
    code = lifecycle.main(
        [
            '--lifecycle-manifest',
            str(lifecycle_manifest),
            '--verification-manifest',
            str(verification_manifest),
            '--verification-report',
            str(verification_report),
            '--json',
            str(json_out),
        ]
    )
    assert code == 0
    payload = json.loads(json_out.read_text(encoding='utf-8'))
    buckets = {c['issue']: c['bucket'] for c in payload['classifications']}
    assert buckets[1] == 'implementation_present_checks_red'
