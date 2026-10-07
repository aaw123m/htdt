"""Append-only persistence for the #791 operational-energy authority.

Four tables in one repository — power-mode observations
(``cad_device_power_mode_observations``), networked-standby evidence
(``cad_networked_standby_evidence``), system scenarios
(``cad_operational_energy_scenarios``) and annualized derivations
(``cad_energy_use_derivations``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_operational_energy import (
    DevicePowerModeObservation,
    EnergyUseDerivation,
    NetworkedStandbyEvidence,
    OperationalEnergyIntegrityError,
    OperationalEnergyScenario,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class OperationalEnergyConflictError(ValueError):
    """An operational-energy save violated append-only identity rules."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise OperationalEnergyIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise OperationalEnergyIntegrityError(
            'record id does not match its sealed sha256')


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
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise OperationalEnergyConflictError(
                f'{self.table} records are append-only')
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
            raise OperationalEnergyIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise OperationalEnergyIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise OperationalEnergyIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise OperationalEnergyIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadOperationalEnergyRepository:
    """Native storage for the #791 operational-energy authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_device_power_mode_observations',
                'cad_networked_standby_evidence',
                'cad_operational_energy_scenarios',
                'cad_energy_use_derivations',
            )
        self.power_mode_observations = _SealedStore(
            self._connect,
            'cad_device_power_mode_observations',
            DevicePowerModeObservation,
            'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('device_ref_id', 'device_ref'),
                _ref('device_state_ref_id', 'device_state_ref'),
                ('mode', 'mode'),
                ('evidence_class', 'evidence_class'),
                ('standard_profile', 'standard_profile'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )
        self.networked_standby_evidence = _SealedStore(
            self._connect,
            'cad_networked_standby_evidence',
            NetworkedStandbyEvidence,
            'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('device_ref_id', 'device_ref'),
                ('standard_profile', 'standard_profile'),
                ('evidence_class', 'evidence_class'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )
        self.scenarios = _SealedStore(
            self._connect,
            'cad_operational_energy_scenarios',
            OperationalEnergyScenario,
            'scenario_id', 'scenario_sha256',
            (
                ('document_id', '__document_id__'),
                ('scenario_kind', 'scenario_kind'),
                ('label', 'label'),
            ),
        )
        self.derivations = _SealedStore(
            self._connect,
            'cad_energy_use_derivations',
            EnergyUseDerivation,
            'derivation_id', 'derivation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('scenario_ref_id', 'scenario_ref'),
                ('derivation_kind', 'derivation_kind'),
                ('derivation_version', 'derivation_version'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def get_observation(
            self, observation_id: str,
    ) -> DevicePowerModeObservation | None:
        return self.power_mode_observations.get(observation_id)

    def get_standby_evidence(
            self, evidence_id: str) -> NetworkedStandbyEvidence | None:
        return self.networked_standby_evidence.get(evidence_id)

    def get_scenario(
            self, scenario_id: str) -> OperationalEnergyScenario | None:
        return self.scenarios.get(scenario_id)

    def get_derivation(
            self, derivation_id: str) -> EnergyUseDerivation | None:
        return self.derivations.get(derivation_id)
