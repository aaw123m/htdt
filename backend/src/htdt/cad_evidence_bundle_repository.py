"""Append-only persistence for the evidence-bundle / integrity-manifest
authority (#610).

Five tables:

* ``cad_evidence_bundles`` — sealed package identities.
* ``cad_evidence_artifacts`` — sealed manifest entries.
* ``cad_evidence_derivation_edges`` — sealed provenance-DAG edges.
* ``cad_evidence_attestations`` — sealed optional attestations.
* ``cad_evidence_bundle_validations`` — sealed validation verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_evidence_bundle import (
    CadBundleAttestation,
    CadBundleValidationVerdict,
    CadDerivationEdge,
    CadEvidenceArtifactEntry,
    CadEvidenceBundle,
)


class EvidenceBundleConflictError(ValueError):
    """An evidence-bundle save violated append-only identity rules."""


class EvidenceBundleIntegrityError(ValueError):
    """A stored evidence-bundle row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise EvidenceBundleIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise EvidenceBundleIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadEvidenceBundleRepository:
    """Native storage for the #610 evidence-bundle records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_evidence_bundles',
                'cad_evidence_artifacts',
                'cad_evidence_derivation_edges',
                'cad_evidence_attestations',
                'cad_evidence_bundle_validations',
            )

    # ------------------------------------------------------------------
    # Bundles

    def save_bundle(self, bundle: CadEvidenceBundle) -> None:
        _assert_sealed(bundle, 'bundle_sha256', 'bundle_id')
        existing = self.get_bundle(bundle.bundle_id)
        if existing is not None:
            if existing.bundle_sha256 == bundle.bundle_sha256:
                return
            raise EvidenceBundleConflictError(
                'evidence bundles are append-only — a corrected package '
                'is a new bundle linked by supersedes_bundle_ref'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_bundles (
                    bundle_id, bundle_sha256, document_id, purpose,
                    status, completeness_profile, reproducibility_level,
                    producer_software, producer_version,
                    manifest_root_sha256, created_at_utc,
                    finalized_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.bundle_id,
                    bundle.bundle_sha256,
                    bundle.document_id,
                    bundle.purpose,
                    bundle.status,
                    bundle.completeness_profile,
                    bundle.reproducibility_level,
                    bundle.producer_software,
                    bundle.producer_version,
                    bundle.manifest_root_sha256,
                    bundle.created_at_utc,
                    bundle.finalized_at_utc,
                    bundle.model_dump_json(),
                ),
            )

    def get_bundle(self, bundle_id: str) -> CadEvidenceBundle | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_evidence_bundles WHERE bundle_id=?',
                (bundle_id,),
            ).fetchone()
        if row is None:
            return None
        bundle = CadEvidenceBundle.model_validate_json(
            row['payload_json']
        )
        if (
            bundle.bundle_id != row['bundle_id']
            or bundle.bundle_sha256 != row['bundle_sha256']
            or bundle.document_id != row['document_id']
            or bundle.purpose != row['purpose']
            or bundle.status != row['status']
            or bundle.completeness_profile != row['completeness_profile']
            or bundle.reproducibility_level
            != row['reproducibility_level']
            or bundle.producer_software != row['producer_software']
            or bundle.producer_version != row['producer_version']
            or bundle.manifest_root_sha256
            != row['manifest_root_sha256']
            or bundle.created_at_utc != row['created_at_utc']
            or bundle.finalized_at_utc != row['finalized_at_utc']
        ):
            raise EvidenceBundleIntegrityError(
                'bundle row disagrees with payload'
            )
        return bundle

    def list_bundles(
        self, document_id: str
    ) -> tuple[CadEvidenceBundle, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_evidence_bundles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEvidenceBundle.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Artifact entries

    def save_artifact(self, entry: CadEvidenceArtifactEntry) -> None:
        _assert_sealed(entry, 'artifact_sha256', 'artifact_id')
        existing = self.get_artifact(entry.artifact_id)
        if existing is not None:
            if existing.artifact_sha256 == entry.artifact_sha256:
                return
            raise EvidenceBundleConflictError(
                'manifest entries are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_artifacts (
                    artifact_id, artifact_sha256, document_id,
                    bundle_id, logical_role, artifact_class, inclusion,
                    package_path, required, rights_sensitivity, digest,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.artifact_id,
                    entry.artifact_sha256,
                    entry.document_id,
                    entry.bundle_id,
                    entry.logical_role,
                    entry.artifact_class,
                    entry.inclusion,
                    entry.package_path,
                    int(entry.required),
                    entry.rights_sensitivity,
                    entry.digest,
                    entry.declared_at_utc,
                    entry.model_dump_json(),
                ),
            )

    def get_artifact(
        self, artifact_id: str
    ) -> CadEvidenceArtifactEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_evidence_artifacts WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        entry = CadEvidenceArtifactEntry.model_validate_json(
            row['payload_json']
        )
        if (
            entry.artifact_id != row['artifact_id']
            or entry.artifact_sha256 != row['artifact_sha256']
            or entry.document_id != row['document_id']
            or entry.bundle_id != row['bundle_id']
            or entry.logical_role != row['logical_role']
            or entry.artifact_class != row['artifact_class']
            or entry.inclusion != row['inclusion']
            or entry.package_path != row['package_path']
            or entry.required != bool(row['required'])
            or entry.rights_sensitivity != row['rights_sensitivity']
            or entry.digest != row['digest']
            or entry.declared_at_utc != row['declared_at_utc']
        ):
            raise EvidenceBundleIntegrityError(
                'artifact row disagrees with payload'
            )
        return entry

    def list_artifacts(
        self,
        document_id: str | None = None,
        *,
        bundle_id: str | None = None,
    ) -> tuple[CadEvidenceArtifactEntry, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if bundle_id is not None:
            clauses.append('bundle_id=?')
            params.append(bundle_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM cad_evidence_artifacts '
                f'WHERE {where} ORDER BY seq ASC',
                tuple(params),
            ).fetchall()
        return tuple(
            CadEvidenceArtifactEntry.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Derivation edges

    def save_edge(self, edge: CadDerivationEdge) -> None:
        _assert_sealed(edge, 'edge_sha256', 'edge_id')
        existing = self.get_edge(edge.edge_id)
        if existing is not None:
            if existing.edge_sha256 == edge.edge_sha256:
                return
            raise EvidenceBundleConflictError(
                'derivation edges are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_derivation_edges (
                    edge_id, edge_sha256, document_id, bundle_id,
                    operation, software_identity, output_artifact_id,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.edge_id,
                    edge.edge_sha256,
                    edge.document_id,
                    edge.bundle_id,
                    edge.operation,
                    edge.software_identity,
                    edge.output_artifact_id,
                    edge.declared_at_utc,
                    edge.model_dump_json(),
                ),
            )

    def get_edge(self, edge_id: str) -> CadDerivationEdge | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_evidence_derivation_edges '
                'WHERE edge_id=?',
                (edge_id,),
            ).fetchone()
        if row is None:
            return None
        edge = CadDerivationEdge.model_validate_json(row['payload_json'])
        if (
            edge.edge_id != row['edge_id']
            or edge.edge_sha256 != row['edge_sha256']
            or edge.document_id != row['document_id']
            or edge.bundle_id != row['bundle_id']
            or edge.operation != row['operation']
            or edge.software_identity != row['software_identity']
            or edge.output_artifact_id != row['output_artifact_id']
            or edge.declared_at_utc != row['declared_at_utc']
        ):
            raise EvidenceBundleIntegrityError(
                'derivation edge row disagrees with payload'
            )
        return edge

    def list_edges(
        self,
        document_id: str | None = None,
        *,
        bundle_id: str | None = None,
    ) -> tuple[CadDerivationEdge, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if bundle_id is not None:
            clauses.append('bundle_id=?')
            params.append(bundle_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM cad_evidence_derivation_edges '
                f'WHERE {where} ORDER BY seq ASC',
                tuple(params),
            ).fetchall()
        return tuple(
            CadDerivationEdge.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Attestations

    def save_attestation(self, attestation: CadBundleAttestation) -> None:
        _assert_sealed(
            attestation, 'attestation_sha256', 'attestation_id'
        )
        existing = self.get_attestation(attestation.attestation_id)
        if existing is not None:
            if (
                existing.attestation_sha256
                == attestation.attestation_sha256
            ):
                return
            raise EvidenceBundleConflictError(
                'attestations are append-only — one signer never '
                'overwrites another attestation'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_attestations (
                    attestation_id, attestation_sha256, document_id,
                    bundle_ref_id, signer_identity, role, signed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attestation.attestation_id,
                    attestation.attestation_sha256,
                    attestation.document_id,
                    attestation.bundle_ref.ref_id,
                    attestation.signer_identity,
                    attestation.role,
                    attestation.signed_at_utc,
                    attestation.model_dump_json(),
                ),
            )

    def get_attestation(
        self, attestation_id: str
    ) -> CadBundleAttestation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_evidence_attestations '
                'WHERE attestation_id=?',
                (attestation_id,),
            ).fetchone()
        if row is None:
            return None
        attestation = CadBundleAttestation.model_validate_json(
            row['payload_json']
        )
        if (
            attestation.attestation_id != row['attestation_id']
            or attestation.attestation_sha256
            != row['attestation_sha256']
            or attestation.document_id != row['document_id']
            or attestation.bundle_ref.ref_id != row['bundle_ref_id']
            or attestation.signer_identity != row['signer_identity']
            or attestation.role != row['role']
            or attestation.signed_at_utc != row['signed_at_utc']
        ):
            raise EvidenceBundleIntegrityError(
                'attestation row disagrees with payload'
            )
        return attestation

    def list_attestations(
        self, document_id: str
    ) -> tuple[CadBundleAttestation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_evidence_attestations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBundleAttestation.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Validation verdicts

    def save_verdict(self, verdict: CadBundleValidationVerdict) -> None:
        _assert_sealed(verdict, 'verdict_sha256', 'verdict_id')
        existing = self.get_verdict(verdict.verdict_id)
        if existing is not None:
            if existing.verdict_sha256 == verdict.verdict_sha256:
                return
            raise EvidenceBundleConflictError(
                'validation verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_bundle_validations (
                    verdict_id, verdict_sha256, document_id,
                    bundle_ref_id, profile, state, validation_version,
                    validated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.verdict_id,
                    verdict.verdict_sha256,
                    verdict.document_id,
                    verdict.bundle_ref.ref_id,
                    verdict.profile,
                    verdict.state,
                    verdict.validation_version,
                    verdict.validated_at_utc,
                    verdict.model_dump_json(),
                ),
            )

    def get_verdict(
        self, verdict_id: str
    ) -> CadBundleValidationVerdict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_evidence_bundle_validations '
                'WHERE verdict_id=?',
                (verdict_id,),
            ).fetchone()
        if row is None:
            return None
        verdict = CadBundleValidationVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            verdict.verdict_id != row['verdict_id']
            or verdict.verdict_sha256 != row['verdict_sha256']
            or verdict.document_id != row['document_id']
            or verdict.bundle_ref.ref_id != row['bundle_ref_id']
            or verdict.profile != row['profile']
            or verdict.state != row['state']
            or verdict.validation_version != row['validation_version']
            or verdict.validated_at_utc != row['validated_at_utc']
        ):
            raise EvidenceBundleIntegrityError(
                'validation verdict row disagrees with payload'
            )
        return verdict

    def list_verdicts(
        self, document_id: str
    ) -> tuple[CadBundleValidationVerdict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_evidence_bundle_validations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBundleValidationVerdict.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadEvidenceBundleRepository',
    'EvidenceBundleConflictError',
    'EvidenceBundleIntegrityError',
]
