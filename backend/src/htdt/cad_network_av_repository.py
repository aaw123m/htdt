"""Append-only persistence for the networked AV transport authority
(#591).

Five tables:

* ``cad_network_av_paths`` — sealed declared topologies (nodes, ports,
  links, VLANs, capacities, PoE roles, LAG).
* ``cad_network_media_flows`` — sealed media/control flow declarations
  bound to one path revision.
* ``cad_network_transport_observations`` — sealed packet-quality,
  utilization, QoS, multicast, redundancy, stress and diagnostic
  observations.
* ``cad_network_timing_observations`` — sealed PTP/clocking evidence.
* ``cad_network_av_qualifications`` — sealed verdicts on the
  discovery -> control -> media ladder.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_network_av import (
    NetworkAVPath,
    NetworkAVQualification,
    NetworkMediaFlow,
    NetworkTimingObservation,
    NetworkTransportObservation,
)


class NetworkAVConflictError(ValueError):
    """A network-AV save violated append-only identity rules."""


class NetworkAVIntegrityError(ValueError):
    """A stored network-AV row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise NetworkAVIntegrityError(
            'record payload does not match its sealed identity'
        )


def _assert_sha(record: object, sha_field: str) -> None:
    """Seal check for records whose id is a versioned label, not a
    ``prefix:sha`` digest (topology paths carry caller-named path_id)."""
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise NetworkAVIntegrityError(
            'record payload does not match its sealed sha256'
        )


class CadNetworkAVRepository:
    """Native storage for network AV paths, flows, observations and
    qualifications."""

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
                'cad_network_av_paths',
                'cad_network_media_flows',
                'cad_network_transport_observations',
                'cad_network_timing_observations',
                'cad_network_av_qualifications',
            )

    # ------------------------------------------------------------------
    # Topology paths

    def save_path(self, path: NetworkAVPath) -> None:
        _assert_sha(path, 'path_sha256')
        existing = self.get_path(path.path_id, path.version)
        if existing is not None:
            if existing.path_sha256 == path.path_sha256:
                return
            raise NetworkAVConflictError(
                'network av paths are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_network_av_paths (
                    path_id, version, path_sha256, document_id, label,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path.path_id,
                    path.version,
                    path.path_sha256,
                    path.document_id,
                    path.label,
                    path.created_at_utc,
                    path.model_dump_json(),
                ),
            )

    def get_path(
        self, path_id: str, version: str
    ) -> NetworkAVPath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_av_paths
                WHERE path_id=? AND version=?
                """,
                (path_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def get_path_revision(
        self, path_id: str, path_sha256: str
    ) -> NetworkAVPath | None:
        """Fetch the exact sealed revision a flow/observation binds."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_av_paths
                WHERE path_id=? AND path_sha256=?
                """,
                (path_id, path_sha256),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def list_paths(
        self, document_id: str
    ) -> tuple[NetworkAVPath, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM cad_network_av_paths
                WHERE document_id=?
                ORDER BY created_at_utc, path_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._path_from_row(row) for row in rows)

    def _path_from_row(self, row: sqlite3.Row) -> NetworkAVPath:
        path = NetworkAVPath.model_validate_json(row['payload_json'])
        if (
            path.path_id != row['path_id']
            or path.version != row['version']
            or path.path_sha256 != row['path_sha256']
            or path.document_id != row['document_id']
            or path.label != row['label']
            or path.created_at_utc != row['created_at_utc']
        ):
            raise NetworkAVIntegrityError(
                'network av path row disagrees with payload'
            )
        return path

    # ------------------------------------------------------------------
    # Media flows

    def save_flow(self, flow: NetworkMediaFlow) -> None:
        _assert_sealed(flow, 'flow_sha256', 'flow_id')
        existing = self.get_flow(flow.flow_id)
        if existing is not None:
            if existing.flow_sha256 == flow.flow_sha256:
                return
            raise NetworkAVConflictError(
                'network media flows are append-only'
            )
        if (
            self.get_path_revision(flow.path_id, flow.path_sha256)
            is None
        ):
            raise NetworkAVIntegrityError(
                'a media flow must reference a persisted path revision'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_network_media_flows (
                    flow_id, flow_sha256, document_id, path_id,
                    path_version, provider_profile, delivery,
                    clock_requirement, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flow.flow_id,
                    flow.flow_sha256,
                    flow.document_id,
                    flow.path_id,
                    flow.path_version,
                    flow.provider_profile,
                    flow.delivery,
                    flow.clock_requirement,
                    flow.created_at_utc,
                    flow.model_dump_json(),
                ),
            )

    def get_flow(self, flow_id: str) -> NetworkMediaFlow | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_media_flows
                WHERE flow_id=?
                """,
                (flow_id,),
            ).fetchone()
        if row is None:
            return None
        flow = NetworkMediaFlow.model_validate_json(row['payload_json'])
        if (
            flow.flow_id != row['flow_id']
            or flow.flow_sha256 != row['flow_sha256']
            or flow.document_id != row['document_id']
            or flow.path_id != row['path_id']
            or flow.path_version != row['path_version']
            or flow.provider_profile != row['provider_profile']
            or flow.delivery != row['delivery']
            or flow.clock_requirement != row['clock_requirement']
            or flow.created_at_utc != row['created_at_utc']
        ):
            raise NetworkAVIntegrityError(
                'network media flow row disagrees with payload'
            )
        return flow

    def list_flows(
        self, document_id: str
    ) -> tuple[NetworkMediaFlow, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_network_media_flows
                WHERE document_id=?
                ORDER BY created_at_utc, flow_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            NetworkMediaFlow.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Transport observations

    def save_transport_observation(
        self, observation: NetworkTransportObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_transport_observation(
            observation.observation_id
        )
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise NetworkAVConflictError(
                'transport observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_network_transport_observations (
                    observation_id, observation_sha256, document_id,
                    path_id, path_version, kind, flow_id,
                    interface_ref, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.path_id,
                    observation.path_version,
                    observation.kind,
                    observation.flow_id,
                    observation.interface_ref,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_transport_observation(
        self, observation_id: str
    ) -> NetworkTransportObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_transport_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = NetworkTransportObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.path_id != row['path_id']
            or observation.path_version != row['path_version']
            or observation.kind != row['kind']
            or observation.flow_id != row['flow_id']
            or observation.interface_ref != row['interface_ref']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise NetworkAVIntegrityError(
                'transport observation row disagrees with payload'
            )
        return observation

    def list_transport_observations(
        self, path_id: str, path_version: str
    ) -> tuple[NetworkTransportObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_network_transport_observations
                WHERE path_id=? AND path_version=?
                ORDER BY observed_at_utc, observation_id
                """,
                (path_id, path_version),
            ).fetchall()
        return tuple(
            NetworkTransportObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Timing observations

    def save_timing_observation(
        self, observation: NetworkTimingObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_timing_observation(
            observation.observation_id
        )
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise NetworkAVConflictError(
                'timing observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_network_timing_observations (
                    observation_id, observation_sha256, document_id,
                    path_id, path_version, ptp_domain, node_state,
                    lock_state, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.path_id,
                    observation.path_version,
                    observation.ptp_domain,
                    observation.node_state,
                    observation.lock_state,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_timing_observation(
        self, observation_id: str
    ) -> NetworkTimingObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_timing_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = NetworkTimingObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.path_id != row['path_id']
            or observation.path_version != row['path_version']
            or observation.ptp_domain != row['ptp_domain']
            or observation.node_state != row['node_state']
            or observation.lock_state != row['lock_state']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise NetworkAVIntegrityError(
                'timing observation row disagrees with payload'
            )
        return observation

    def list_timing_observations(
        self, path_id: str, path_version: str
    ) -> tuple[NetworkTimingObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_network_timing_observations
                WHERE path_id=? AND path_version=?
                ORDER BY observed_at_utc, observation_id
                """,
                (path_id, path_version),
            ).fetchall()
        return tuple(
            NetworkTimingObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: NetworkAVQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(
            qualification.qualification_id
        )
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise NetworkAVConflictError(
                'network av qualifications are append-only'
            )
        if (
            self.get_flow(qualification.flow_id) is None
        ):
            raise NetworkAVIntegrityError(
                'a qualification must reference a persisted flow'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_network_av_qualifications (
                    qualification_id, qualification_sha256,
                    document_id, path_id, path_version, flow_id,
                    flow_sha256,
                    media_state, verdict, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.path_id,
                    qualification.path_version,
                    qualification.flow_id,
                    qualification.flow_sha256,
                    qualification.media_state,
                    qualification.verdict,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> NetworkAVQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_network_av_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = NetworkAVQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.path_id != row['path_id']
            or qualification.path_version != row['path_version']
            or qualification.flow_id != row['flow_id']
            or qualification.flow_sha256 != row['flow_sha256']
            or qualification.media_state != row['media_state']
            or qualification.verdict != row['verdict']
            or qualification.evaluated_at_utc
            != row['evaluated_at_utc']
        ):
            raise NetworkAVIntegrityError(
                'network av qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[NetworkAVQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_network_av_qualifications
                WHERE document_id=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            NetworkAVQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadNetworkAVRepository',
    'NetworkAVConflictError',
    'NetworkAVIntegrityError',
]
