"""Append-only persistence for evidence attestation / trusted timestamp
authority (#725, REV59-DEPS).

Three tables:

* ``cad_signed_manifests`` — sealed canonical signed-manifest identities.
* ``cad_manifest_attestations`` — sealed attestations over manifests
  (and attestation chains).
* ``cad_attestation_verifications`` — sealed verification verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_evidence_attestation import (
    AttestationVerification,
    EvidenceAttestation,
    SignedManifestRecord,
)


class AttestationConflictError(ValueError):
    """An attestation save violated append-only identity."""


class AttestationIntegrityError(ValueError):
    """A stored attestation row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AttestationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise AttestationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadEvidenceAttestationRepository:
    """Native storage for the #725 attestation records."""

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
                'cad_signed_manifests',
                'cad_manifest_attestations',
                'cad_attestation_verifications',
            )

    # ------------------------------------------------------------------
    # Signed manifests

    def save_manifest(self, record: SignedManifestRecord) -> None:
        _assert_sealed(record, 'manifest_record_sha256', 'manifest_id')
        existing = self.get_manifest(record.manifest_id)
        if existing is not None:
            if (
                existing.manifest_record_sha256
                == record.manifest_record_sha256
            ):
                return
            raise AttestationConflictError(
                'signed manifests are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_signed_manifests (
                    manifest_id, manifest_record_sha256, document_id,
                    manifest_label, manifest_sha256, approval_scope,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.manifest_id,
                    record.manifest_record_sha256,
                    record.document_id,
                    record.manifest_label,
                    record.manifest_sha256,
                    record.approval_scope,
                    record.created_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_manifest(
        self, manifest_id: str
    ) -> SignedManifestRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_signed_manifests WHERE manifest_id=?',
                (manifest_id,),
            ).fetchone()
        if row is None:
            return None
        record = SignedManifestRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.manifest_id != row['manifest_id']
            or record.manifest_record_sha256
            != row['manifest_record_sha256']
            or record.document_id != row['document_id']
            or record.manifest_label != row['manifest_label']
            or record.manifest_sha256 != row['manifest_sha256']
            or record.approval_scope != row['approval_scope']
            or record.created_at_utc != row['created_at_utc']
        ):
            raise AttestationIntegrityError(
                'signed manifest row disagrees with payload'
            )
        return record

    def list_manifests(
        self, document_id: str
    ) -> tuple[SignedManifestRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_signed_manifests '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            SignedManifestRecord.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Evidence attestations

    def save_attestation(self, attestation: EvidenceAttestation) -> None:
        _assert_sealed(
            attestation, 'attestation_sha256', 'attestation_id'
        )
        existing = self.get_attestation(attestation.attestation_id)
        if existing is not None:
            if existing.attestation_sha256 == attestation.attestation_sha256:
                return
            raise AttestationConflictError(
                'evidence attestations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_manifest_attestations (
                    attestation_id, attestation_sha256, document_id,
                    manifest_ref_id, kind, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attestation.attestation_id,
                    attestation.attestation_sha256,
                    attestation.document_id,
                    attestation.manifest_ref.ref_id,
                    attestation.kind,
                    attestation.declared_at_utc,
                    attestation.model_dump_json(),
                ),
            )

    def get_attestation(
        self, attestation_id: str
    ) -> EvidenceAttestation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_manifest_attestations '
                'WHERE attestation_id=?',
                (attestation_id,),
            ).fetchone()
        if row is None:
            return None
        attestation = EvidenceAttestation.model_validate_json(
            row['payload_json']
        )
        if (
            attestation.attestation_id != row['attestation_id']
            or attestation.attestation_sha256
            != row['attestation_sha256']
            or attestation.document_id != row['document_id']
            or attestation.manifest_ref.ref_id != row['manifest_ref_id']
            or attestation.kind != row['kind']
            or attestation.declared_at_utc != row['declared_at_utc']
        ):
            raise AttestationIntegrityError(
                'evidence attestation row disagrees with payload'
            )
        return attestation

    def list_attestations(
        self, document_id: str
    ) -> tuple[EvidenceAttestation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_manifest_attestations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            EvidenceAttestation.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Verifications

    def save_verification(
        self, verification: AttestationVerification
    ) -> None:
        _assert_sealed(
            verification, 'verification_sha256', 'verification_id'
        )
        existing = self.get_verification(verification.verification_id)
        if existing is not None:
            if (
                existing.verification_sha256
                == verification.verification_sha256
            ):
                return
            raise AttestationConflictError(
                'attestation verifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_attestation_verifications (
                    verification_id, verification_sha256, document_id,
                    attestation_ref_id, manifest_ref_id, state,
                    time_authority, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification.verification_id,
                    verification.verification_sha256,
                    verification.document_id,
                    verification.attestation_ref.ref_id,
                    verification.manifest_ref.ref_id,
                    verification.state,
                    verification.time_authority,
                    verification.evaluated_at_utc,
                    verification.model_dump_json(),
                ),
            )

    def get_verification(
        self, verification_id: str
    ) -> AttestationVerification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_attestation_verifications '
                'WHERE verification_id=?',
                (verification_id,),
            ).fetchone()
        if row is None:
            return None
        verification = AttestationVerification.model_validate_json(
            row['payload_json']
        )
        if (
            verification.verification_id != row['verification_id']
            or verification.verification_sha256
            != row['verification_sha256']
            or verification.document_id != row['document_id']
            or verification.attestation_ref.ref_id
            != row['attestation_ref_id']
            or verification.manifest_ref.ref_id
            != row['manifest_ref_id']
            or verification.state != row['state']
            or verification.time_authority != row['time_authority']
            or verification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise AttestationIntegrityError(
                'attestation verification row disagrees with payload'
            )
        return verification

    def list_verifications(
        self, document_id: str
    ) -> tuple[AttestationVerification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_attestation_verifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            AttestationVerification.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'AttestationConflictError',
    'AttestationIntegrityError',
    'CadEvidenceAttestationRepository',
]
