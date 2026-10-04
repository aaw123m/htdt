"""Production ``.htdtcapture`` import path for native HTDT (#333).

This is the only supported entry point for ingesting an untrusted
capture artifact produced by HTDT-Capture. The pipeline is strictly
staged so every failure carries a machine-readable ``stage``:

1. ``read`` — the artifact is opened as a ZIP wrapper or bundle
   directory under bounded limits (entries, sizes, compression ratio).
2. ``manifest`` — the exact ``manifest.json`` bytes are parsed,
   canonical-checked, schema-validated, and digested.
3. ``validate`` — every declared payload is validated against the
   pinned Capture Bundle v1 schema/binary/metadata contract, and all
   handoff semantics are rederived from the exact bytes.
4. ``plan`` — the canonical ingestion plan is built by the pinned
   reference-ingestor contract.
5. ``commit`` — a single SQLite transaction registers the immutable
   capture revision, retains the canonical manifest, and persists all
   source-authority rows — or none on failure.

Re-importing the same artifact is idempotent: the commit layer
re-verifies the persisted materialization and returns ``created=False``
with the same digests. Nothing here promotes anything to the semantic
scene — that remains a separate explicit operator decision — and the
slice has no Windows/RDC dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from hashlib import sha256
from pathlib import Path

from htdt.capture_bundle import (
    CaptureBundleError,
    FrozenBundle,
)
from htdt.capture_ingestion_transaction import (
    CaptureIngestionCommitResult,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
    CapturePayloadContractError,
)
from htdt.capture_reference import (
    CaptureIngestionContractError,
    build_ingestion_plan,
)


class CaptureImportError(ValueError):
    """A capture import failure carrying its pipeline stage."""

    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f'capture import {stage} failed: {reason}')


@dataclass(frozen=True)
class CaptureImportResult:
    """Report of one import attempt (new or verified-reimport)."""

    stage: str
    bundle_digest: str
    capture_revision_id: str
    capture_series_id: str
    lineage_digest: str
    source_evidence_count: int
    roomplan_record_count: int
    raw_mesh_binding_count: int
    authority_record_count: int
    created: bool
    quality_state: str
    quality_ruleset_version: str | None
    app_name: str
    app_version: str
    app_build: str


def _envelope_sha256(artifact: Path) -> str:
    """Digest of the received envelope bytes.

    For a ``.htdtcapture`` archive this is the file hash; for an extracted
    bundle directory it is the manifest hash when readable (the strongest
    bundle identity) and otherwise a hash over the sorted
    ``relpath:sha256`` listing — deterministic either way so a re-delivered
    byte-identical rejection resolves to the same inbox row.
    """
    if artifact.is_file():
        return sha256(artifact.read_bytes()).hexdigest()
    manifest = artifact / 'manifest.json'
    if manifest.is_file():
        return sha256(manifest.read_bytes()).hexdigest()
    parts: list[str] = []
    for child in sorted(artifact.rglob('*')):
        if child.is_file():
            parts.append(
                f'{child.relative_to(artifact)}:{sha256(child.read_bytes()).hexdigest()}'
            )
    return sha256('\n'.join(parts).encode('utf-8')).hexdigest()


def import_capture_artifact(
    path: Path | str,
    repository: CaptureIngestionRepository,
    *,
    budget=None,
    inbox_repository=None,
    arrival_source: str = 'cli_import',
) -> CaptureImportResult:
    """Import one ``.htdtcapture`` ZIP wrapper or bundle directory.

    ``path`` may name a ``.htdtcapture`` file or an extracted bundle
    directory. ``repository`` is the standard transaction facade —
    callers own its lifecycle (the SQLite database is opened under the
    repository's SceneRepository and all rows commit atomically). When
    ``inbox_repository`` is given, read/manifest/validate rejections are
    staged as rejected-envelope inbox rows before the error is raised, so
    failed deliveries stay operator-visible instead of vanishing into a
    log line.
    """
    artifact = Path(path)

    def _stage_rejected(reason: str) -> None:
        if inbox_repository is None:
            return
        try:
            inbox_repository.stage_rejected(
                envelope_sha256=_envelope_sha256(artifact),
                validation_error=reason,
                arrival_source=arrival_source,
                source_detail=str(artifact),
            )
        except Exception:
            # Staging is audit, never the reason an import fails — the
            # original rejection is still reported to the caller.
            pass

    try:
        frozen = FrozenBundle(artifact)
    except (
        CaptureBundleError,
        ValueError,
        FileNotFoundError,
        RecursionError,  # deep-but-parseable payloads can still escape the re-walk
    ) as exc:
        _stage_rejected(f'read: {exc}')
        raise CaptureImportError('read', str(exc)) from exc

    report = frozen.report
    manifest_bytes = frozen.manifest_bytes
    if sha256(manifest_bytes).hexdigest() != report['bundle_digest']:
        _stage_rejected(
            'manifest: manifest SHA-256 does not equal the bundle digest'
        )
        raise CaptureImportError(
            'manifest',
            'manifest SHA-256 does not equal the bundle digest',
        )

    try:
        manifest_document = json.loads(manifest_bytes)
    except ValueError as exc:
        _stage_rejected(f'manifest: {exc}')
        raise CaptureImportError('manifest', str(exc)) from exc

    try:
        plan = build_ingestion_plan(frozen)
    except CaptureIngestionContractError as exc:
        _stage_rejected(f'validate: {exc}')
        raise CaptureImportError('validate', str(exc)) from exc

    payloads = {
        entry['path']: frozen.read(entry['path'])
        for entry in manifest_document['files']
    }

    try:
        commit = repository.ingest(
            plan,
            payloads,
            budget=budget,
            manifest=manifest_bytes,
        )
    except (
        CaptureIngestionTransactionError,
        CapturePayloadContractError,
    ) as exc:
        raise CaptureImportError('commit', str(exc)) from exc

    return _result(manifest_document, commit)


def _result(
    manifest: dict,
    commit: CaptureIngestionCommitResult,
) -> CaptureImportResult:
    app = manifest['app']
    return CaptureImportResult(
        stage='committed' if commit.created else 'verified',
        bundle_digest=commit.bundle_digest,
        capture_revision_id=commit.capture_revision_id,
        capture_series_id=commit.capture_series_id,
        lineage_digest=commit.lineage_digest,
        source_evidence_count=commit.source_evidence_count,
        roomplan_record_count=commit.roomplan_record_count,
        raw_mesh_binding_count=commit.raw_mesh_binding_count,
        authority_record_count=commit.authority_record_count,
        created=commit.created,
        quality_state=commit.quality_state,
        quality_ruleset_version=commit.quality_ruleset_version,
        app_name=app['name'],
        app_version=app['version'],
        app_build=app['build'],
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: ``htdt-capture-import <artifact> --db <cad.sqlite3>``."""
    import argparse
    import sys

    from htdt.cad_repository import SceneRepository

    parser = argparse.ArgumentParser(
        prog='htdt-capture-import',
        description='Import a .htdtcapture artifact into an HTDT database',
    )
    parser.add_argument('artifact', help='path to .htdtcapture or bundle dir')
    parser.add_argument(
        '--db',
        required=True,
        help='path to the HTDT SQLite database',
    )
    args = parser.parse_args(argv)

    repository = CaptureIngestionRepository(SceneRepository(Path(args.db)))
    from htdt.capture_inbox import CaptureInboxRepository

    inbox = CaptureInboxRepository(repository.scene_repository, repository)
    try:
        result = import_capture_artifact(
            args.artifact, repository, inbox_repository=inbox
        )
    except CaptureImportError as exc:
        print(
            json.dumps({'stage': exc.stage, 'error': exc.reason}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
