"""Issue #849 — release manifest publisher-signature status merge.

The PowerShell signing stage (``sign-release.ps1``) owns ``signtool``;
this module owns the manifest contract: a ``publisher_signature`` block
recorded NEXT TO — never inside — the ``verification`` block, so a code
signature can never be read as software/correctness verification.

Semantics enforced here:

* ``software_verification`` stays whatever #833 recorded — signing never
  mutates it;
* ``publisher_signature.status`` is one of ``signed_verified`` /
  ``unsigned`` / ``signing_failed`` / ``unverifiable``;
* the manifest's artifact sha256 is recomputed from the file on disk —
  report-claimed hashes are never trusted;
* ``pre_sign_sha256`` vs ``signed_sha256`` stay distinct identities
  (signing embeds timestamp/signature material — byte reproducibility
  across signing is not claimed);
* ``--require-signed`` makes anything but ``signed_verified`` fatal;
* no secret field (PFX password, key material) is accepted or echoed —
  the report schema has no secret surface at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SIGNATURE_STATUSES = {
    'signed_verified',
    'unsigned',
    'signing_failed',
    'unverifiable',
}

#: Fields every report must carry as non-empty values. ``signed_sha256``
#: is deliberately absent: only a ``signed_verified`` report may carry a
#: digest there — ``sign-release.ps1`` emits ``''`` for every other
#: status, and those reports must still load so their honest
#: unsigned/failed/unverifiable status can be merged.
REPORT_REQUIRED_FIELDS = {
    'file': str,
    'status': str,
    'pre_sign_sha256': str,
}

#: Field names that must never appear in a signature report — the report
#: schema has no secret surface by construction (issue #849 secret isolation).
FORBIDDEN_REPORT_FIELDS = {
    'password', 'pfx_password', 'private_key', 'key_material', 'secret',
    'cert_password', 'credential',
}

MANIFEST_SCHEMA = 'htdt-release-manifest/1'


class SignatureMergeError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_signature_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignatureMergeError(f'signature report unreadable: {exc}')
    if not isinstance(report, dict):
        raise SignatureMergeError('signature report must be a JSON object')
    forbidden = FORBIDDEN_REPORT_FIELDS & set(report)
    if forbidden:
        raise SignatureMergeError(
            f'signature report must not contain secret fields: {sorted(forbidden)}'
        )
    for field_name, field_type in REPORT_REQUIRED_FIELDS.items():
        value = report.get(field_name)
        if not isinstance(value, field_type) or not str(value).strip():
            raise SignatureMergeError(
                f'signature report missing/invalid field: {field_name}'
            )
    if report['status'] not in SIGNATURE_STATUSES:
        raise SignatureMergeError(
            f"signature status must be one of {sorted(SIGNATURE_STATUSES)}"
        )
    # signed_sha256 is conditional on the outcome: a signtool-verified
    # report must carry the post-sign digest; every other status must
    # carry it empty. A report contradicting itself in either direction
    # is rejected rather than silently reinterpreted.
    signed_sha = report.get('signed_sha256')
    if not isinstance(signed_sha, str):
        raise SignatureMergeError(
            'signature report missing/invalid field: signed_sha256'
        )
    if report['status'] == 'signed_verified' and not signed_sha.strip():
        raise SignatureMergeError(
            'signed_verified report must carry a signed_sha256 digest'
        )
    if report['status'] != 'signed_verified' and signed_sha.strip():
        raise SignatureMergeError(
            f"{report['status']} report must not carry signed_sha256"
        )
    return report


def merge_signature_status(
    manifest_path: Path,
    report: dict[str, Any],
    *,
    artifact_path: Path | None = None,
    require_signed: bool = False,
) -> dict[str, Any]:
    """Write the publisher_signature block into a release manifest.

    Returns the updated manifest dict (also persisted to ``manifest_path``).
    The artifact sha256 recorded in the manifest is recomputed from disk
    when ``artifact_path`` exists — never trusted from the report alone.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignatureMergeError(f'release manifest unreadable: {exc}')
    if manifest.get('schema') != MANIFEST_SCHEMA:
        raise SignatureMergeError(
            f'unexpected manifest schema: {manifest.get("schema")!r}'
        )

    status = report['status']
    file_name = report['file']

    if artifact_path is not None and artifact_path.is_file():
        actual_sha = _sha256(artifact_path)
    else:
        # Off-host merge (e.g. CI artifact not present): identity comes
        # from the report, marked accordingly.
        actual_sha = report['signed_sha256'] if status == 'signed_verified' else report['pre_sign_sha256']

    if status == 'signed_verified':
        if artifact_path is not None and artifact_path.is_file():
            if actual_sha != report['signed_sha256'].lower():
                raise SignatureMergeError(
                    'signed artifact sha256 mismatch — manifest will not '
                    'bind a different binary than signtool verified'
                )
        if not report.get('signer_subject'):
            raise SignatureMergeError(
                'signed_verified requires signer_subject (publisher identity)'
            )

    verified = status == 'signed_verified'
    manifest['publisher_signature'] = {
        'status': status,
        'artifact': file_name,
        'pre_sign_sha256': report['pre_sign_sha256'].lower(),
        'signed_sha256': (
            report['signed_sha256'].lower() if verified else None
        ),
        'artifact_sha256': actual_sha,
        # Signer identity/timestamp are only recorded when a signature
        # actually verified — a failed/unsigned report claiming a signer
        # is meaningless and must not surface as publisher identity.
        'signer_subject': report.get('signer_subject') if verified else None,
        'signer_thumbprint': report.get('signer_thumbprint') if verified else None,
        'timestamped': bool(report.get('timestamped')) if verified else False,
        'timestamp_url': report.get('timestamp_url') if verified else None,
        # Explicitly NOT a correctness claim.
        'scope': 'publisher_identity_only',
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )

    if require_signed and status != 'signed_verified':
        raise SignatureMergeError(
            f'signature required but publisher_signature.status={status!r}'
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True,
                        help='release manifest JSON to update')
    parser.add_argument('--signature-report', type=Path, required=True,
                        help='signature-report.json from sign-release.ps1')
    parser.add_argument('--artifact', type=Path, default=None,
                        help='the artifact on disk — sha256 is recomputed')
    parser.add_argument('--require-signed', action='store_true',
                        help='fail unless publisher_signature is signed_verified')
    args = parser.parse_args(argv)

    try:
        report = load_signature_report(args.signature_report)
        manifest = merge_signature_status(
            args.manifest,
            report,
            artifact_path=args.artifact,
            require_signed=args.require_signed,
        )
    except SignatureMergeError as exc:
        print(f'release_signature: {exc}', file=sys.stderr)
        return 2
    print(
        f"release_signature: publisher_signature="
        f"{manifest['publisher_signature']['status']} for "
        f"{manifest['publisher_signature']['artifact']}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
