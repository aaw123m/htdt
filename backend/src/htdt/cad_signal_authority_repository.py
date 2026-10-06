"""Append-only persistence for REV59-SIGNAL authorities.

Six tables — codec fidelity (#747/#753), spectral estimator (#749),
clock domains (#670), external fact claims (#765), fact conflict
resolution (#765), BOM/estimate (#667).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_codec_fidelity import CodecFidelityEvidence
from .cad_spectral_clock import (
    ClockDomainObservation,
    SpectralEstimatorProfile,
)
from .cad_fact_bom import (
    BomEstimate,
    ExternalFactClaim,
    FactConflictResolution,
)


class SignalAuthorityConflictError(ValueError):
    """A SIGNAL save violated append-only identity rules."""


class SignalAuthorityIntegrityError(ValueError):
    """A stored SIGNAL row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SignalAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SignalAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise SignalAuthorityConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise SignalAuthorityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise SignalAuthorityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise SignalAuthorityIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadSignalAuthorityRepository:
    """Native storage for the #747/#753/#749/#670/#765/#667
    authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_codec_fidelity_evidence',
                'cad_fft_spectral_estimator_profiles',
                'cad_clock_domain_observations',
                'cad_external_fact_claims',
                'cad_fact_conflict_resolutions',
                'cad_bom_estimates',
            )

        self.codec_evidence = _SealedStore(
            self._connect, 'cad_codec_fidelity_evidence',
            CodecFidelityEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('media_kind', 'media_kind'),
                ('codec_family', 'codec_family'),
            ),
        )

        self.spectral_profiles = _SealedStore(
            self._connect, 'cad_fft_spectral_estimator_profiles',
            SpectralEstimatorProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('window_kind', 'window_kind'),
                ('enbw_bins', 'enbw_bins'),
            ),
        )

        self.clock_observations = _SealedStore(
            self._connect, 'cad_clock_domain_observations',
            ClockDomainObservation, 'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('domain_kind', 'domain_kind'),
                ('lock_state', 'lock_state'),
            ),
        )

        self.fact_claims = _SealedStore(
            self._connect, 'cad_external_fact_claims',
            ExternalFactClaim, 'claim_id', 'claim_sha256',
            (
                ('document_id', '__document_id__'),
                ('subject', 'subject'),
                ('published_on', 'published_on'),
            ),
        )

        self.fact_resolutions = _SealedStore(
            self._connect, 'cad_fact_conflict_resolutions',
            FactConflictResolution, 'resolution_id', 'resolution_sha256',
            (
                ('document_id', '__document_id__'),
                ('resolution_kind', 'resolution_kind'),
            ),
        )

        self.bom_estimates = _SealedStore(
            self._connect, 'cad_bom_estimates',
            BomEstimate, 'estimate_id', 'estimate_sha256',
            (
                ('document_id', '__document_id__'),
                ('bom_version', 'bom_version'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_codec_evidence(self, record: CodecFidelityEvidence) -> None:
        self.codec_evidence.save(record)

    def get_codec_evidence(self, rid: str) -> CodecFidelityEvidence | None:
        return self.codec_evidence.get(rid)

    def save_spectral_profile(self, record: SpectralEstimatorProfile) -> None:
        self.spectral_profiles.save(record)

    def get_spectral_profile(self, rid: str) -> SpectralEstimatorProfile | None:
        return self.spectral_profiles.get(rid)

    def save_clock_observation(self, record: ClockDomainObservation) -> None:
        self.clock_observations.save(record)

    def get_clock_observation(self, rid: str) -> ClockDomainObservation | None:
        return self.clock_observations.get(rid)

    def save_fact_claim(self, record: ExternalFactClaim) -> None:
        self.fact_claims.save(record)

    def get_fact_claim(self, rid: str) -> ExternalFactClaim | None:
        return self.fact_claims.get(rid)

    def save_fact_resolution(self, record: FactConflictResolution) -> None:
        self.fact_resolutions.save(record)

    def get_fact_resolution(self, rid: str) -> FactConflictResolution | None:
        return self.fact_resolutions.get(rid)

    def save_bom_estimate(self, record: BomEstimate) -> None:
        self.bom_estimates.save(record)

    def get_bom_estimate(self, rid: str) -> BomEstimate | None:
        return self.bom_estimates.get(rid)
