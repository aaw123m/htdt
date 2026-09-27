from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_video_geometry import (
    ProjectorSpecification,
    ProjectorSpecificationEvidence,
    VideoGeometryEvaluation,
    evaluate_video_geometry,
    verify_projector_specification_evidence,
)
from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetStore,
    verify_managed_asset,
)
from .clock import utc_now_iso as _utc_now


class ProjectorSpecSourceAssetMetadata(BaseModel):
    """Import-context metadata for one retained projector-spec source asset.

    Byte identity lives in the shared managed asset registry
    (``cad_measurement_assets`` row plus the digest-named file); this record
    preserves filename/media context and the evidence record the asset is
    bound to, separately from the content address.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    filename: str | None = Field(default=None, min_length=1)
    media_type: str | None = Field(default=None, min_length=1)
    size_bytes: int = Field(ge=0)
    recorded_at_utc: str = Field(min_length=1)


class CadVideoGeometryRepository:
    """Append-only projector specification and geometry-evaluation persistence.

    A ``ProjectorSpecification`` is only production-authoritative when its
    typed provenance ref resolves: every save and every authoritative read
    re-resolves the persisted ``ProjectorSpecificationEvidence`` record,
    verifies retained source bytes against the managed asset store where
    evidence claims them, and re-derives the specification's optical payload
    from the evidence assertions. Missing, tampered or diverging evidence
    fails closed instead of serving unsupported optical limits.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
        *,
        assets_dir: Path | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = Path(scene_repository.path)
        # Resolve any interrupted managed-data restore before the asset store
        # can create its directory inside the managed data root.
        ensure_native_schema(self.path)
        self.assets_dir = (
            Path(assets_dir)
            if assets_dir is not None
            else self.path.parent / MANAGED_ASSETS_DIRNAME
        )
        self._asset_store = ManagedAssetStore(self.assets_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_projector_specifications', 'cad_measurement_assets', 'cad_projector_spec_evidence', 'cad_projector_spec_source_assets', 'cad_video_geometry_evaluations')

    def _verified_asset_file(
        self,
        digest: str,
        *,
        relative_path: str,
        size_bytes: int,
    ) -> bytes:
        """Reopen a registered managed asset, failing closed on tampering."""
        target = verify_managed_asset(
            data_dir=self.path.parent,
            digest=digest,
            relative_path=relative_path,
            size_bytes=size_bytes,
        )
        raw = self._asset_store.read_file(target)
        if sha256(raw).hexdigest() != digest:
            raise ValueError(
                'managed projector spec source asset SHA-256 mismatch'
            )
        return raw

    def _source_asset_row(self, digest: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT relative_path, size_bytes
                FROM cad_measurement_assets
                WHERE sha256=?
                """,
                (digest,),
            ).fetchone()

    def _verified_source_asset(self, digest: str) -> bytes:
        """Reopen the exact bound source bytes for *digest*.

        A registered asset whose file is missing, resized or tampered fails
        closed — the evidence is never served without its exact source bytes.
        """
        row = self._source_asset_row(digest)
        if row is None:
            raise ValueError(
                'projector spec source asset is not registered in the '
                'managed asset store'
            )
        return self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def _verify_evidence_assets(
        self,
        evidence: ProjectorSpecificationEvidence,
    ) -> None:
        """Fail closed when retained evidence bytes cannot be verified.

        ``manufacturer_document`` evidence must resolve to a registered and
        byte-exact managed asset. Other kinds may additionally declare a
        ``source_sha256``: when a managed asset is registered for that digest
        it is verified the same way, so tampered retained bytes are never
        treated as clean evidence.
        """
        digest = evidence.source_sha256
        if digest is None:
            if evidence.evidence_kind == 'manufacturer_document':
                raise ValueError(
                    'manufacturer_document evidence requires a source_sha256'
                )
            return
        row = self._source_asset_row(digest)
        if row is None:
            if evidence.evidence_kind == 'manufacturer_document':
                raise ValueError(
                    'projector spec evidence source asset is not registered '
                    'in the managed asset store'
                )
            return
        self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def save_projector_spec_evidence(
        self,
        evidence: ProjectorSpecificationEvidence,
        *,
        source_bytes: bytes | None = None,
        source_filename: str | None = None,
        media_type: str | None = None,
    ) -> ProjectorSpecificationEvidence:
        """Persist a typed evidence record, optionally retaining source bytes.

        ``source_bytes`` are the exact source document/data bytes the
        evidence was extracted from: they must hash to
        ``evidence.source_sha256`` and are installed atomically into the
        shared managed asset store under that digest. When ``source_bytes``
        is omitted, a declared ``source_sha256`` may bind to an
        already-registered managed asset, which is re-verified before commit.
        ``manufacturer_document`` evidence is never persisted without
        retained and verified source bytes — a bare hash is not evidence.
        """
        evidence = ProjectorSpecificationEvidence.model_validate(
            evidence.model_dump(mode='python')
        )
        digest = evidence.source_sha256
        bound_source: bytes | None = None
        if source_bytes is not None:
            if not isinstance(source_bytes, bytes):
                raise TypeError('projector spec source asset payload must be bytes')
            if digest is None or sha256(source_bytes).hexdigest() != digest:
                raise ValueError(
                    'projector spec source bytes do not match '
                    'evidence source_sha256'
                )
            if not source_filename:
                raise ValueError(
                    'projector spec source filename is required when '
                    'persisting source bytes'
                )
            # Install before the transaction: a failed commit leaves a safe
            # content-addressed orphan rather than a partially written file.
            self._asset_store.ensure_installed(digest, source_bytes)
            bound_source = source_bytes
        elif digest is not None and self._source_asset_row(digest) is not None:
            bound_source = self._verified_source_asset(digest)
        if (
            evidence.evidence_kind == 'manufacturer_document'
            and bound_source is None
        ):
            raise ValueError(
                'manufacturer_document evidence requires the exact retained '
                'source bytes or a registered managed asset'
            )

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_spec_evidence
                WHERE evidence_sha256=?
                """,
                (evidence.evidence_sha256,),
            ).fetchone()
            if existing is not None:
                persisted = ProjectorSpecificationEvidence.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evidence:
                    raise ValueError(
                        'projector spec evidence sha256 exists with different semantics'
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO cad_projector_spec_evidence(
                        evidence_sha256, evidence_kind, source_sha256,
                        manufacturer, model, payload_json, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.evidence_sha256,
                        evidence.evidence_kind,
                        evidence.source_sha256,
                        evidence.manufacturer,
                        evidence.model,
                        evidence.model_dump_json(),
                        _utc_now(),
                    ),
                )
            if bound_source is not None:
                target = self._asset_store.asset_path(evidence.source_sha256)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        evidence.source_sha256,
                        source_filename or '',
                        str(target.relative_to(self.path.parent)),
                        len(bound_source),
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cad_projector_spec_source_assets(
                        source_asset_sha256, evidence_sha256,
                        filename, media_type, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.source_sha256,
                        evidence.evidence_sha256,
                        source_filename,
                        media_type,
                        _utc_now(),
                    ),
                )
        return evidence

    def get_projector_spec_evidence(
        self,
        evidence_sha256: str,
    ) -> ProjectorSpecificationEvidence | None:
        """Resolve a persisted evidence record, verifying retained bytes.

        Returns ``None`` only when no evidence is registered for the digest;
        a record whose retained source asset is missing, resized or tampered
        raises instead of returning unverifiable evidence.
        """
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_spec_evidence
                WHERE evidence_sha256=?
                """,
                (evidence_sha256,),
            ).fetchone()
        if row is None:
            return None
        evidence = ProjectorSpecificationEvidence.model_validate_json(
            row['payload_json']
        )
        self._verify_evidence_assets(evidence)
        return evidence

    def read_projector_spec_source_asset(
        self,
        source_asset_sha256: str,
    ) -> bytes | None:
        """Reopen exact retained source bytes for a content address.

        Returns ``None`` only when no managed asset is registered for the
        digest; a registered asset whose file is missing, resized or
        tampered raises instead of returning unverifiable bytes.
        """
        row = self._source_asset_row(source_asset_sha256)
        if row is None:
            return None
        return self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def get_projector_spec_source_metadata(
        self,
        source_asset_sha256: str,
    ) -> ProjectorSpecSourceAssetMetadata | None:
        """Return preserved filename/media metadata for a retained asset.

        The bound managed file is re-verified before metadata is served, so
        tampered evidence never comes back with clean provenance.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT a.evidence_sha256, a.filename, a.media_type,
                       a.recorded_at_utc, m.size_bytes, m.relative_path
                FROM cad_projector_spec_source_assets a
                JOIN cad_measurement_assets m
                    ON m.sha256 = a.source_asset_sha256
                WHERE a.source_asset_sha256=?
                """,
                (source_asset_sha256,),
            ).fetchone()
        if row is None:
            return None
        self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )
        return ProjectorSpecSourceAssetMetadata(
            source_asset_sha256=source_asset_sha256,
            evidence_sha256=row['evidence_sha256'],
            filename=row['filename'],
            media_type=row['media_type'],
            size_bytes=int(row['size_bytes']),
            recorded_at_utc=row['recorded_at_utc'],
        )

    def _resolve_specification_evidence(
        self,
        specification: ProjectorSpecification,
    ) -> ProjectorSpecificationEvidence:
        evidence = self.get_projector_spec_evidence(
            specification.provenance.evidence.evidence_sha256
        )
        if evidence is None:
            raise ValueError(
                'projector specification references unpersisted evidence'
            )
        verify_projector_specification_evidence(
            specification=specification,
            evidence=evidence,
        )
        return evidence

    def _verified_specification(
        self,
        payload_json: str,
    ) -> ProjectorSpecification:
        """Authoritative read: re-resolve evidence and re-derive the spec."""
        specification = ProjectorSpecification.model_validate_json(payload_json)
        self._resolve_specification_evidence(specification)
        return specification

    def save_projector_specification(
        self,
        specification: ProjectorSpecification,
        *,
        evidence: ProjectorSpecificationEvidence | None = None,
        source_bytes: bytes | None = None,
        source_filename: str | None = None,
        media_type: str | None = None,
    ) -> ProjectorSpecification:
        """Persist a specification bound to resolved typed evidence.

        The typed provenance ref must resolve: pass ``evidence`` (plus
        ``source_bytes``/``source_filename`` for retained-document evidence)
        to persist it atomically with the specification, or persist it first
        through ``save_projector_spec_evidence``. The resolved record must
        re-derive every optical value the specification claims; fabricated
        references, evidence for another manufacturer/model/document or
        missing retained bytes all fail closed before anything is written.
        """
        specification = ProjectorSpecification.model_validate(
            specification.model_dump(mode='python')
        )
        ref = specification.provenance.evidence
        if evidence is not None:
            if (
                evidence.evidence_sha256 != ref.evidence_sha256
                or evidence.evidence_kind != ref.evidence_kind
            ):
                raise ValueError(
                    'supplied evidence does not match the specification '
                    'provenance ref'
                )
            evidence = self.save_projector_spec_evidence(
                evidence,
                source_bytes=source_bytes,
                source_filename=source_filename,
                media_type=media_type,
            )
        else:
            if any(
                item is not None
                for item in (source_bytes, source_filename, media_type)
            ):
                raise ValueError(
                    'source bytes/metadata require the matching evidence record'
                )
            evidence = self.get_projector_spec_evidence(ref.evidence_sha256)
        if evidence is None:
            raise ValueError(
                'projector specification references unpersisted evidence'
            )
        verify_projector_specification_evidence(
            specification=specification,
            evidence=evidence,
        )

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_id=? AND version=?
                """,
                (specification.specification_id, specification.version),
            ).fetchone()
            if existing is not None:
                persisted = ProjectorSpecification.model_validate_json(
                    existing['payload_json']
                )
                if persisted != specification:
                    raise ValueError(
                        'projector specification id/version already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_projector_specifications(
                    specification_id, version, specification_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    specification.specification_id,
                    specification.version,
                    specification.specification_sha256,
                    specification.model_dump_json(),
                    _utc_now(),
                ),
            )
        return specification

    def get_projector_specification(
        self,
        specification_id: str,
        version: str,
    ) -> ProjectorSpecification | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_id=? AND version=?
                """,
                (specification_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else self._verified_specification(row['payload_json'])
        )

    def get_projector_specification_by_hash(
        self,
        specification_sha256: str,
    ) -> ProjectorSpecification | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_sha256=?
                """,
                (specification_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else self._verified_specification(row['payload_json'])
        )

    def list_projector_specifications(self) -> tuple[ProjectorSpecification, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_projector_specifications ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            self._verified_specification(row['payload_json'])
            for row in rows
        )

    def _reproduce_evaluation(
        self,
        evaluation: VideoGeometryEvaluation,
        specification: ProjectorSpecification,
    ) -> VideoGeometryEvaluation:
        target = evaluation.target
        baseline = self.scene_repository.get(target.scene_revision_id)
        if baseline is None:
            raise ValueError('video geometry SceneRevision does not exist')
        if (
            baseline.document_id != target.document_id
            or baseline.content_hash != target.scene_content_hash
        ):
            raise ValueError('video geometry SceneRevision authority mismatch')

        variant = None
        if target.system_variant_id is not None:
            if self.variant_repository is None:
                raise ValueError(
                    'SystemVariant-bound geometry evaluation requires variant repository'
                )
            variant = self.variant_repository.get_variant(target.system_variant_id)
            if variant is None:
                raise ValueError('video geometry SystemVariant does not exist')
            if variant.variant_sha256 != target.system_variant_sha256:
                raise ValueError('video geometry SystemVariant hash mismatch')
            if (
                variant.document_id != target.document_id
                or variant.baseline_revision_id != target.scene_revision_id
                or variant.baseline_content_hash != target.scene_content_hash
            ):
                raise ValueError('video geometry SystemVariant baseline mismatch')

        reproduced = evaluate_video_geometry(
            baseline=baseline,
            variant=variant,
            projector_specification=specification,
            request=evaluation.request,
        )
        if reproduced != evaluation:
            raise ValueError(
                'video geometry evaluation is not reproducible from exact persisted authority'
            )
        return reproduced

    def _specification_for(
        self,
        evaluation: VideoGeometryEvaluation,
    ) -> ProjectorSpecification:
        specification = self.get_projector_specification_by_hash(
            evaluation.projector_specification_sha256
        )
        if specification is None:
            raise ValueError(
                'video geometry evaluation references an unpersisted projector specification'
            )
        return specification

    def _replay_persisted_evaluation(
        self,
        payload_json: str,
    ) -> VideoGeometryEvaluation:
        evaluation = VideoGeometryEvaluation.model_validate_json(payload_json)
        return self._reproduce_evaluation(
            evaluation,
            self._specification_for(evaluation),
        )

    def save_evaluation(
        self,
        evaluation: VideoGeometryEvaluation,
    ) -> VideoGeometryEvaluation:
        evaluation = VideoGeometryEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        reproduced = self._reproduce_evaluation(
            evaluation,
            self._specification_for(evaluation),
        )

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE evaluation_id=?
                """,
                (reproduced.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = VideoGeometryEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != reproduced:
                    raise ValueError(
                        'video geometry evaluation id already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_video_geometry_evaluations(
                    evaluation_id, evaluation_sha256, document_id,
                    scene_revision_id, scene_content_hash,
                    system_variant_id, system_variant_sha256,
                    projector_specification_sha256, request_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reproduced.evaluation_id,
                    reproduced.evaluation_sha256,
                    reproduced.target.document_id,
                    reproduced.target.scene_revision_id,
                    reproduced.target.scene_content_hash,
                    reproduced.target.system_variant_id,
                    reproduced.target.system_variant_sha256,
                    reproduced.projector_specification_sha256,
                    reproduced.request.request_sha256,
                    reproduced.model_dump_json(),
                    _utc_now(),
                ),
            )
        return reproduced

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> VideoGeometryEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._replay_persisted_evaluation(row['payload_json'])

    def list_evaluations_for_revision(
        self,
        scene_revision_id: str,
    ) -> tuple[VideoGeometryEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE scene_revision_id=?
                ORDER BY seq ASC
                """,
                (scene_revision_id,),
            ).fetchall()
        return tuple(
            self._replay_persisted_evaluation(row['payload_json'])
            for row in rows
        )

    def list_evaluations_for_variant(
        self,
        system_variant_id: str,
    ) -> tuple[VideoGeometryEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE system_variant_id=?
                ORDER BY seq ASC
                """,
                (system_variant_id,),
            ).fetchall()
        return tuple(
            self._replay_persisted_evaluation(row['payload_json'])
            for row in rows
        )
