"""Append-only persistence for the measurement timebase / clock
authority (#609).

Three tables:

* ``cad_timebase_clock_domains`` — sealed clock-domain declarations.
* ``cad_measurement_timebases`` — sealed per-capture timebase
  authorities.
* ``cad_timebase_capability_assessments`` — sealed fail-closed
  capability verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_timebase_authority import (
    CadClockDomain,
    CadMeasurementTimebase,
    CadTimebaseCapabilityAssessment,
)


class TimebaseAuthorityConflictError(ValueError):
    """A timebase-authority save violated append-only identity rules."""


class TimebaseAuthorityIntegrityError(ValueError):
    """A stored timebase row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TimebaseAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TimebaseAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadTimebaseAuthorityRepository:
    """Native storage for the #609 timebase-authority records."""

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
                'cad_timebase_clock_domains',
                'cad_measurement_timebases',
                'cad_timebase_capability_assessments',
            )

    # ------------------------------------------------------------------
    # Clock domains

    def save_clock_domain(self, domain: CadClockDomain) -> None:
        _assert_sealed(domain, 'clock_domain_sha256', 'clock_domain_id')
        existing = self.get_clock_domain(domain.clock_domain_id)
        if existing is not None:
            if existing.clock_domain_sha256 == domain.clock_domain_sha256:
                return
            raise TimebaseAuthorityConflictError(
                'clock domains are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_timebase_clock_domains (
                    clock_domain_id, clock_domain_sha256, document_id,
                    domain_kind, device_identity,
                    nominal_sample_rate_hz, effective_sample_rate_hz,
                    common_clock_group, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain.clock_domain_id,
                    domain.clock_domain_sha256,
                    domain.document_id,
                    domain.domain_kind,
                    domain.device_identity,
                    domain.nominal_sample_rate_hz,
                    domain.effective_sample_rate_hz,
                    domain.common_clock_group,
                    domain.declared_at_utc,
                    domain.model_dump_json(),
                ),
            )

    def get_clock_domain(
        self, clock_domain_id: str
    ) -> CadClockDomain | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_timebase_clock_domains '
                'WHERE clock_domain_id=?',
                (clock_domain_id,),
            ).fetchone()
        if row is None:
            return None
        domain = CadClockDomain.model_validate_json(row['payload_json'])
        if (
            domain.clock_domain_id != row['clock_domain_id']
            or domain.clock_domain_sha256 != row['clock_domain_sha256']
            or domain.document_id != row['document_id']
            or domain.domain_kind != row['domain_kind']
            or domain.device_identity != row['device_identity']
            or domain.nominal_sample_rate_hz
            != row['nominal_sample_rate_hz']
            or domain.effective_sample_rate_hz
            != row['effective_sample_rate_hz']
            or domain.common_clock_group != row['common_clock_group']
            or domain.declared_at_utc != row['declared_at_utc']
        ):
            raise TimebaseAuthorityIntegrityError(
                'clock domain row disagrees with payload'
            )
        return domain

    def list_clock_domains(
        self, document_id: str
    ) -> tuple[CadClockDomain, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_timebase_clock_domains '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadClockDomain.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Measurement timebases

    def save_timebase(self, timebase: CadMeasurementTimebase) -> None:
        _assert_sealed(timebase, 'timebase_sha256', 'timebase_id')
        existing = self.get_timebase(timebase.timebase_id)
        if existing is not None:
            if existing.timebase_sha256 == timebase.timebase_sha256:
                return
            raise TimebaseAuthorityConflictError(
                'measurement timebases are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_timebases (
                    timebase_id, timebase_sha256, document_id,
                    topology, topology_evidence, sequential_anchor,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timebase.timebase_id,
                    timebase.timebase_sha256,
                    timebase.document_id,
                    timebase.topology,
                    timebase.topology_evidence,
                    timebase.sequential_anchor,
                    timebase.declared_at_utc,
                    timebase.model_dump_json(),
                ),
            )

    def get_timebase(
        self, timebase_id: str
    ) -> CadMeasurementTimebase | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_measurement_timebases '
                'WHERE timebase_id=?',
                (timebase_id,),
            ).fetchone()
        if row is None:
            return None
        timebase = CadMeasurementTimebase.model_validate_json(
            row['payload_json']
        )
        if (
            timebase.timebase_id != row['timebase_id']
            or timebase.timebase_sha256 != row['timebase_sha256']
            or timebase.document_id != row['document_id']
            or timebase.topology != row['topology']
            or timebase.topology_evidence != row['topology_evidence']
            or timebase.sequential_anchor != row['sequential_anchor']
            or timebase.declared_at_utc != row['declared_at_utc']
        ):
            raise TimebaseAuthorityIntegrityError(
                'timebase row disagrees with payload'
            )
        return timebase

    def list_timebases(
        self, document_id: str
    ) -> tuple[CadMeasurementTimebase, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_timebases '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMeasurementTimebase.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Capability assessments

    def save_assessment(
        self, assessment: CadTimebaseCapabilityAssessment
    ) -> None:
        _assert_sealed(assessment, 'assessment_sha256', 'assessment_id')
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise TimebaseAuthorityConflictError(
                'capability assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_timebase_capability_assessments (
                    assessment_id, assessment_sha256, document_id,
                    timebase_ref_id, timing_uncertainty_s, drift_material,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.timebase_ref.ref_id,
                    assessment.timing_uncertainty_s,
                    (
                        None
                        if assessment.drift_material is None
                        else int(assessment.drift_material)
                    ),
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadTimebaseCapabilityAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_timebase_capability_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = CadTimebaseCapabilityAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.timebase_ref.ref_id != row['timebase_ref_id']
            or assessment.timing_uncertainty_s
            != row['timing_uncertainty_s']
            or assessment.drift_material != (
                None if row['drift_material'] is None
                else bool(row['drift_material'])
            )
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise TimebaseAuthorityIntegrityError(
                'assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[CadTimebaseCapabilityAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_timebase_capability_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTimebaseCapabilityAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadTimebaseAuthorityRepository',
    'TimebaseAuthorityConflictError',
    'TimebaseAuthorityIntegrityError',
]
