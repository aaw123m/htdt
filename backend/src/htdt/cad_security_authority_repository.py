"""Append-only persistence for the networked AV security authority
(#598).

Nine tables:

* ``cad_security_assets`` — sealed security overlays on existing
  equipment/network subjects (reachability, firmware support state,
  cloud/certificate dependencies, lifecycle, backup sensitivity).
* ``cad_security_credentials`` — sealed account/credential metadata
  records (references and status only — never secret material).
* ``cad_security_surfaces`` — sealed management-surface declarations.
* ``cad_security_observations`` — sealed security evidence records.
* ``cad_security_risks`` — sealed qualitative risk records with
  mitigation and functional-AV-effect fields.
* ``cad_remote_service_authorizations`` — sealed remote-service consent
  and scope records.
* ``cad_security_test_evidence`` — sealed authorized-review evidence.
* ``cad_access_reviews`` — sealed access review/revocation records.
* ``cad_security_reviews`` — sealed profile evaluation verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_security_authority import (
    AccessReviewRecord,
    CredentialRecord,
    ManagementSurfaceDeclaration,
    RemoteServiceAuthorization,
    SecurityAssetDeclaration,
    SecurityObservation,
    SecurityReview,
    SecurityRiskRecord,
    SecurityTestEvidence,
)


class SecurityAuthorityConflictError(ValueError):
    """A security-authority save violated append-only identity rules."""


class SecurityAuthorityIntegrityError(ValueError):
    """A stored security-authority row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SecurityAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SecurityAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSecurityAuthorityRepository:
    """Native storage for the #598 security authority records."""

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
                'cad_security_assets',
                'cad_security_credentials',
                'cad_security_surfaces',
                'cad_security_observations',
                'cad_security_risks',
                'cad_remote_service_authorizations',
                'cad_security_test_evidence',
                'cad_access_reviews',
                'cad_security_reviews',
            )

    # ------------------------------------------------------------------
    # Shared helpers

    def _list(
        self,
        *,
        table: str,
        model,
        where: str,
        params: tuple[object, ...],
        order: str,
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                f'ORDER BY {order}',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Security asset declarations

    def save_asset(self, asset: SecurityAssetDeclaration) -> None:
        _assert_sealed(asset, 'asset_sha256', 'asset_id')
        existing = self.get_asset(asset.asset_id)
        if existing is not None:
            if existing.asset_sha256 == asset.asset_sha256:
                return
            raise SecurityAuthorityConflictError(
                'security assets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_assets (
                    asset_id, asset_sha256, document_id,
                    subject_kind, subject_ref_id, management_reachability,
                    vendor_support_status, lifecycle_state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    asset.asset_sha256,
                    asset.document_id,
                    asset.subject_ref.kind,
                    asset.subject_ref.ref_id,
                    asset.management_reachability,
                    asset.vendor_support_status,
                    asset.lifecycle_state,
                    asset.declared_at_utc,
                    asset.model_dump_json(),
                ),
            )

    def get_asset(self, asset_id: str) -> SecurityAssetDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_assets WHERE asset_id=?',
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        asset = SecurityAssetDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            asset.asset_id != row['asset_id']
            or asset.asset_sha256 != row['asset_sha256']
            or asset.document_id != row['document_id']
            or asset.management_reachability
            != row['management_reachability']
            or asset.vendor_support_status != row['vendor_support_status']
            or asset.lifecycle_state != row['lifecycle_state']
            or asset.declared_at_utc != row['declared_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'security asset row disagrees with payload'
            )
        return asset

    def list_assets(
        self, document_id: str
    ) -> tuple[SecurityAssetDeclaration, ...]:
        return self._list(
            table='cad_security_assets',
            model=SecurityAssetDeclaration,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, asset_id',
        )

    # ------------------------------------------------------------------
    # Credential records

    def save_credential(self, credential: CredentialRecord) -> None:
        _assert_sealed(credential, 'credential_sha256', 'credential_id')
        existing = self.get_credential(credential.credential_id)
        if existing is not None:
            if existing.credential_sha256 == credential.credential_sha256:
                return
            raise SecurityAuthorityConflictError(
                'credential records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_credentials (
                    credential_id, credential_sha256, document_id,
                    subject_kind, subject_ref_id, account_ref, kind,
                    scope, default_credential_state, state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential.credential_id,
                    credential.credential_sha256,
                    credential.document_id,
                    credential.subject_ref.kind,
                    credential.subject_ref.ref_id,
                    credential.account_ref,
                    credential.kind,
                    credential.scope,
                    credential.default_credential_state,
                    credential.state,
                    credential.declared_at_utc,
                    credential.model_dump_json(),
                ),
            )

    def get_credential(
        self, credential_id: str
    ) -> CredentialRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_credentials '
                'WHERE credential_id=?',
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        credential = CredentialRecord.model_validate_json(
            row['payload_json']
        )
        if (
            credential.credential_id != row['credential_id']
            or credential.credential_sha256 != row['credential_sha256']
            or credential.document_id != row['document_id']
            or credential.account_ref != row['account_ref']
            or credential.kind != row['kind']
            or credential.scope != row['scope']
            or credential.default_credential_state
            != row['default_credential_state']
            or credential.state != row['state']
            or credential.declared_at_utc != row['declared_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'credential row disagrees with payload'
            )
        return credential

    def list_credentials(
        self, document_id: str
    ) -> tuple[CredentialRecord, ...]:
        return self._list(
            table='cad_security_credentials',
            model=CredentialRecord,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, credential_id',
        )

    # ------------------------------------------------------------------
    # Management surfaces

    def save_surface(
        self, surface: ManagementSurfaceDeclaration
    ) -> None:
        _assert_sealed(surface, 'surface_sha256', 'surface_id')
        existing = self.get_surface(surface.surface_id)
        if existing is not None:
            if existing.surface_sha256 == surface.surface_sha256:
                return
            raise SecurityAuthorityConflictError(
                'management surfaces are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_surfaces (
                    surface_id, surface_sha256, document_id,
                    subject_kind, subject_ref_id, kind, state,
                    exposure_scope, authentication_state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    surface.surface_id,
                    surface.surface_sha256,
                    surface.document_id,
                    surface.subject_ref.kind,
                    surface.subject_ref.ref_id,
                    surface.kind,
                    surface.state,
                    surface.exposure_scope,
                    surface.authentication_state,
                    surface.declared_at_utc,
                    surface.model_dump_json(),
                ),
            )

    def get_surface(
        self, surface_id: str
    ) -> ManagementSurfaceDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_surfaces WHERE surface_id=?',
                (surface_id,),
            ).fetchone()
        if row is None:
            return None
        surface = ManagementSurfaceDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            surface.surface_id != row['surface_id']
            or surface.surface_sha256 != row['surface_sha256']
            or surface.document_id != row['document_id']
            or surface.kind != row['kind']
            or surface.state != row['state']
            or surface.exposure_scope != row['exposure_scope']
            or surface.authentication_state != row['authentication_state']
            or surface.declared_at_utc != row['declared_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'management surface row disagrees with payload'
            )
        return surface

    def list_surfaces(
        self, document_id: str
    ) -> tuple[ManagementSurfaceDeclaration, ...]:
        return self._list(
            table='cad_security_surfaces',
            model=ManagementSurfaceDeclaration,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, surface_id',
        )

    # ------------------------------------------------------------------
    # Security observations

    def save_observation(self, observation: SecurityObservation) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise SecurityAuthorityConflictError(
                'security observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_observations (
                    observation_id, observation_sha256, document_id,
                    subject_kind, subject_ref_id, kind, outcome,
                    evidence_class, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.subject_ref.kind,
                    observation.subject_ref.ref_id,
                    observation.kind,
                    observation.outcome,
                    observation.evidence_class,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> SecurityObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = SecurityObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.kind != row['kind']
            or observation.outcome != row['outcome']
            or observation.evidence_class != row['evidence_class']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'security observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[SecurityObservation, ...]:
        return self._list(
            table='cad_security_observations',
            model=SecurityObservation,
            where='document_id=?',
            params=(document_id,),
            order='observed_at_utc, observation_id',
        )

    # ------------------------------------------------------------------
    # Risk records

    def save_risk(self, risk: SecurityRiskRecord) -> None:
        _assert_sealed(risk, 'risk_sha256', 'risk_id')
        existing = self.get_risk(risk.risk_id)
        if existing is not None:
            if existing.risk_sha256 == risk.risk_sha256:
                return
            raise SecurityAuthorityConflictError(
                'security risks are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_risks (
                    risk_id, risk_sha256, document_id,
                    subject_kind, subject_ref_id, title,
                    likelihood_class, status, review_at_utc,
                    raised_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    risk.risk_id,
                    risk.risk_sha256,
                    risk.document_id,
                    risk.subject_ref.kind if risk.subject_ref else None,
                    risk.subject_ref.ref_id if risk.subject_ref else None,
                    risk.title,
                    risk.likelihood_class,
                    risk.status,
                    risk.review_at_utc,
                    risk.raised_at_utc,
                    risk.model_dump_json(),
                ),
            )

    def get_risk(self, risk_id: str) -> SecurityRiskRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_risks WHERE risk_id=?',
                (risk_id,),
            ).fetchone()
        if row is None:
            return None
        risk = SecurityRiskRecord.model_validate_json(row['payload_json'])
        if (
            risk.risk_id != row['risk_id']
            or risk.risk_sha256 != row['risk_sha256']
            or risk.document_id != row['document_id']
            or risk.title != row['title']
            or risk.likelihood_class != row['likelihood_class']
            or risk.status != row['status']
            or risk.review_at_utc != row['review_at_utc']
            or risk.raised_at_utc != row['raised_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'security risk row disagrees with payload'
            )
        return risk

    def list_risks(
        self, document_id: str
    ) -> tuple[SecurityRiskRecord, ...]:
        return self._list(
            table='cad_security_risks',
            model=SecurityRiskRecord,
            where='document_id=?',
            params=(document_id,),
            order='raised_at_utc, risk_id',
        )

    # ------------------------------------------------------------------
    # Remote service authorizations

    def save_remote_authorization(
        self, authorization: RemoteServiceAuthorization
    ) -> None:
        _assert_sealed(
            authorization, 'authorization_sha256', 'authorization_id'
        )
        existing = self.get_remote_authorization(
            authorization.authorization_id
        )
        if existing is not None:
            if (
                existing.authorization_sha256
                == authorization.authorization_sha256
            ):
                return
            raise SecurityAuthorityConflictError(
                'remote service authorizations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_remote_service_authorizations (
                    authorization_id, authorization_sha256, document_id,
                    subject_kind, subject_ref_id, method, state,
                    valid_until_utc, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization.authorization_id,
                    authorization.authorization_sha256,
                    authorization.document_id,
                    authorization.subject_ref.kind,
                    authorization.subject_ref.ref_id,
                    authorization.method,
                    authorization.state,
                    authorization.valid_until_utc,
                    authorization.declared_at_utc,
                    authorization.model_dump_json(),
                ),
            )

    def get_remote_authorization(
        self, authorization_id: str
    ) -> RemoteServiceAuthorization | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_remote_service_authorizations '
                'WHERE authorization_id=?',
                (authorization_id,),
            ).fetchone()
        if row is None:
            return None
        authorization = RemoteServiceAuthorization.model_validate_json(
            row['payload_json']
        )
        if (
            authorization.authorization_id != row['authorization_id']
            or authorization.authorization_sha256
            != row['authorization_sha256']
            or authorization.document_id != row['document_id']
            or authorization.method != row['method']
            or authorization.state != row['state']
            or authorization.valid_until_utc != row['valid_until_utc']
            or authorization.declared_at_utc != row['declared_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'remote authorization row disagrees with payload'
            )
        return authorization

    def list_remote_authorizations(
        self, document_id: str
    ) -> tuple[RemoteServiceAuthorization, ...]:
        return self._list(
            table='cad_remote_service_authorizations',
            model=RemoteServiceAuthorization,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, authorization_id',
        )

    # ------------------------------------------------------------------
    # Security test evidence

    def save_test_evidence(self, evidence: SecurityTestEvidence) -> None:
        _assert_sealed(evidence, 'evidence_sha256', 'evidence_id')
        existing = self.get_test_evidence(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise SecurityAuthorityConflictError(
                'security test evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_test_evidence (
                    evidence_id, evidence_sha256, document_id, kind,
                    tool_provider, performed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    evidence.kind,
                    evidence.tool_provider,
                    evidence.performed_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_test_evidence(
        self, evidence_id: str
    ) -> SecurityTestEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_test_evidence '
                'WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = SecurityTestEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or evidence.kind != row['kind']
            or evidence.tool_provider != row['tool_provider']
            or evidence.performed_at_utc != row['performed_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'security test evidence row disagrees with payload'
            )
        return evidence

    def list_test_evidence(
        self, document_id: str
    ) -> tuple[SecurityTestEvidence, ...]:
        return self._list(
            table='cad_security_test_evidence',
            model=SecurityTestEvidence,
            where='document_id=?',
            params=(document_id,),
            order='performed_at_utc, evidence_id',
        )

    # ------------------------------------------------------------------
    # Access reviews

    def save_access_review(self, review: AccessReviewRecord) -> None:
        _assert_sealed(review, 'review_sha256', 'review_id')
        existing = self.get_access_review(review.review_id)
        if existing is not None:
            if existing.review_sha256 == review.review_sha256:
                return
            raise SecurityAuthorityConflictError(
                'access reviews are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_access_reviews (
                    review_id, review_sha256, document_id, trigger,
                    reviewer, performed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.review_sha256,
                    review.document_id,
                    review.trigger,
                    review.reviewer,
                    review.performed_at_utc,
                    review.model_dump_json(),
                ),
            )

    def get_access_review(
        self, review_id: str
    ) -> AccessReviewRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_access_reviews WHERE review_id=?',
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        review = AccessReviewRecord.model_validate_json(
            row['payload_json']
        )
        if (
            review.review_id != row['review_id']
            or review.review_sha256 != row['review_sha256']
            or review.document_id != row['document_id']
            or review.trigger != row['trigger']
            or review.reviewer != row['reviewer']
            or review.performed_at_utc != row['performed_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'access review row disagrees with payload'
            )
        return review

    def list_access_reviews(
        self, document_id: str
    ) -> tuple[AccessReviewRecord, ...]:
        return self._list(
            table='cad_access_reviews',
            model=AccessReviewRecord,
            where='document_id=?',
            params=(document_id,),
            order='performed_at_utc, review_id',
        )

    # ------------------------------------------------------------------
    # Security reviews

    def save_review(self, review: SecurityReview) -> None:
        _assert_sealed(review, 'review_sha256', 'review_id')
        existing = self.get_review(review.review_id)
        if existing is not None:
            if existing.review_sha256 == review.review_sha256:
                return
            raise SecurityAuthorityConflictError(
                'security reviews are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_security_reviews (
                    review_id, review_sha256, document_id, state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.review_sha256,
                    review.document_id,
                    review.state,
                    review.evaluation_version,
                    review.evaluated_at_utc,
                    review.model_dump_json(),
                ),
            )

    def get_review(self, review_id: str) -> SecurityReview | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_security_reviews WHERE review_id=?',
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        review = SecurityReview.model_validate_json(row['payload_json'])
        if (
            review.review_id != row['review_id']
            or review.review_sha256 != row['review_sha256']
            or review.document_id != row['document_id']
            or review.state != row['state']
            or review.evaluation_version != row['evaluation_version']
            or review.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SecurityAuthorityIntegrityError(
                'security review row disagrees with payload'
            )
        return review

    def list_reviews(
        self, document_id: str
    ) -> tuple[SecurityReview, ...]:
        return self._list(
            table='cad_security_reviews',
            model=SecurityReview,
            where='document_id=?',
            params=(document_id,),
            order='evaluated_at_utc, review_id',
        )


__all__ = [
    'CadSecurityAuthorityRepository',
    'SecurityAuthorityConflictError',
    'SecurityAuthorityIntegrityError',
]
