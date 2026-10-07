"""#849 — optional Authenticode signing stage / publisher-signature manifest.

Signing is publisher identity, never a correctness gate: the
``publisher_signature`` block lives next to ``verification`` and cannot
promote any solver/UX/physical/software state. The ps1 owns signtool; this
module's merge contract is exercised fully offline.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sig_mod = _load('release_signature', ROOT / 'scripts' / 'release_signature.py')


def _manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / 'HTDT-Setup-1.0.0.manifest.json'
    manifest.write_text(json.dumps({
        'schema': 'htdt-release-manifest/1',
        'application_version': '1.0.0',
        'commit_sha': 'a' * 40,
        'installer': {'file': 'HTDT-Setup-1.0.0.exe', 'sha256': 'pre'},
        'verification': {'status': 'verified', 'evidence_sha256': 'e' * 64},
    }), encoding='utf-8')
    return manifest


def _report(tmp_path: Path, **over) -> dict:
    report = {
        'file': 'HTDT-Setup-1.0.0.exe',
        'status': 'signed_verified',
        'pre_sign_sha256': 'b' * 64,
        'signed_sha256': 'c' * 64,
        'signer_subject': 'CN=HTDT Maintainer',
        'signer_thumbprint': 'D' * 40,
        'timestamped': True,
        'timestamp_url': 'http://timestamp.digicert.com',
    }
    report.update(over)
    return report


# --- merge semantics -----------------------------------------------------


def test_signed_merge_records_publisher_block_and_preserves_verification(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / 'HTDT-Setup-1.0.0.exe'
    artifact.write_bytes(b'signed-bytes')
    signed_sha = hashlib.sha256(b'signed-bytes').hexdigest()
    manifest_path = _manifest(tmp_path)

    manifest = sig_mod.merge_signature_status(
        manifest_path,
        _report(tmp_path, signed_sha256=signed_sha),
        artifact_path=artifact,
    )
    sig = manifest['publisher_signature']
    assert sig['status'] == 'signed_verified'
    assert sig['signer_subject'] == 'CN=HTDT Maintainer'
    assert sig['artifact_sha256'] == signed_sha
    assert sig['pre_sign_sha256'] == 'b' * 64
    assert sig['scope'] == 'publisher_identity_only'
    # Signing never mutates software verification.
    assert manifest['verification'] == {
        'status': 'verified', 'evidence_sha256': 'e' * 64,
    }


def test_signed_sha256_is_recomputed_not_trusted(tmp_path: Path) -> None:
    artifact = tmp_path / 'HTDT-Setup-1.0.0.exe'
    artifact.write_bytes(b'actual-bytes')
    manifest_path = _manifest(tmp_path)

    with pytest.raises(sig_mod.SignatureMergeError, match='sha256 mismatch'):
        sig_mod.merge_signature_status(
            manifest_path,
            _report(tmp_path, signed_sha256='c' * 64),  # claims other bytes
            artifact_path=artifact,
        )


def test_signed_verified_requires_signer_identity(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    with pytest.raises(sig_mod.SignatureMergeError, match='signer_subject'):
        sig_mod.merge_signature_status(
            manifest_path, _report(tmp_path, signer_subject=None),
        )


def test_unsigned_build_is_valid_and_visibly_distinct(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = sig_mod.merge_signature_status(
        manifest_path, _report(tmp_path, status='unsigned'),
    )
    sig = manifest['publisher_signature']
    assert sig['status'] == 'unsigned'
    assert sig['signed_sha256'] is None
    assert sig['signer_subject'] is None
    # Signed vs unsigned distinguishable from the manifest alone.
    assert manifest['verification']['status'] == 'verified'


def test_require_signed_fails_closed(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    for status in ('unsigned', 'signing_failed', 'unverifiable'):
        with pytest.raises(sig_mod.SignatureMergeError,
                           match='signature required'):
            sig_mod.merge_signature_status(
                manifest_path,
                _report(tmp_path, status=status),
                require_signed=True,
            )


# --- report validation ---------------------------------------------------


def test_report_rejects_missing_fields(tmp_path: Path) -> None:
    report_path = tmp_path / 'sig.json'
    report_path.write_text(json.dumps({'file': 'x.exe'}), encoding='utf-8')
    with pytest.raises(sig_mod.SignatureMergeError, match='missing/invalid'):
        sig_mod.load_signature_report(report_path)


def test_report_rejects_unknown_status(tmp_path: Path) -> None:
    report_path = tmp_path / 'sig.json'
    report_path.write_text(json.dumps(_report(tmp_path, status='ok')),
                           encoding='utf-8')
    with pytest.raises(sig_mod.SignatureMergeError, match='status'):
        sig_mod.load_signature_report(report_path)


def test_report_has_no_secret_surface(tmp_path: Path) -> None:
    report_path = tmp_path / 'sig.json'
    report_path.write_text(
        json.dumps(_report(tmp_path, pfx_password='hunter2')),
        encoding='utf-8',
    )
    with pytest.raises(sig_mod.SignatureMergeError, match='secret fields'):
        sig_mod.load_signature_report(report_path)


def test_manifest_schema_is_pinned(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest_path.write_text(json.dumps({'schema': 'other/2'}),
                             encoding='utf-8')
    with pytest.raises(sig_mod.SignatureMergeError, match='schema'):
        sig_mod.merge_signature_status(
            manifest_path, _report(tmp_path, status='unsigned'),
        )


def test_signing_stage_script_is_signtool_only() -> None:
    """sign-release.ps1 uses signtool + cert store/PFX env, never repo secrets."""
    text = (ROOT / 'scripts' / 'sign-release.ps1').read_text(encoding='utf-8')
    assert 'signtool' in text
    assert 'HTDT_SIGN_PFX_PASSWORD' in text
    assert 'RequireSigning' in text
    # PFX password comes only from the env var and is never written into a
    # report field or echoed.
    assert "$env:HTDT_SIGN_PFX_PASSWORD" in text
    assert "$report.password" not in text.lower()
    assert "report['password'" not in text.lower()


def test_workflow_signing_stage_is_optional_and_secret_isolated() -> None:
    """build-windows-artifacts.yml wires an opt-in Authenticode stage."""
    text = (ROOT / '.github' / 'workflows' / 'build-windows-artifacts.yml'
            ).read_text(encoding='utf-8')
    assert 'require_signature' in text
    assert 'sign-release.ps1' in text
    assert 'release_signature.py' in text
    # Signing capability arrives via secrets, never literals in the repo.
    assert 'HTDT_CODESIGN_PFX_BASE64' in text
    assert 'secrets.HTDT_CODESIGN' in text
    # The stage runs only on explicit opt-in.
    assert 'if: inputs.require_signature' in text
