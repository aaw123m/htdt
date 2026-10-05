"""Append-only persistence for the rack/power/thermal infrastructure
authority (#587).

Eight tables:

* ``cad_rack_enclosures`` — sealed rack/enclosure declarations.
* ``cad_rack_devices`` — sealed installed-device thermal/electrical
  evidence; a device may only be persisted against a stored rack whose
  sha matches ``rack_ref``.
* ``cad_branch_circuits`` — sealed declared supply circuits.
* ``cad_power_protection_devices`` — sealed protection devices (UPS,
  surge, regulation, sequencing, monitoring — a capability set, never
  a marketing label).
* ``cad_poe_budgets`` — sealed PoE per-port + aggregate budgets.
* ``cad_infrastructure_scenarios`` — sealed operating scenarios used as
  qualification context.
* ``cad_rack_thermal_measurements`` — sealed measured thermal/electrical
  states bound to a stored rack (and optionally a stored scenario).
* ``cad_infrastructure_qualifications`` — sealed per-scenario verdicts
  produced by ``evaluate_infrastructure``; the bound scenario must be
  persisted first.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_infrastructure import (
    BranchCircuit,
    InfrastructureQualification,
    InfrastructureScenario,
    InstalledRackDevice,
    PoEBudget,
    PowerProtectionDevice,
    RackEnclosure,
    RackThermalMeasurement,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class InfrastructureConflictError(ValueError):
    """An infrastructure save violated append-only identity rules."""


class InfrastructureIntegrityError(ValueError):
    """A stored infrastructure row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise InfrastructureIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise InfrastructureIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadInfrastructureRepository:
    """Native storage for the #587 infrastructure authority records."""

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
                'cad_rack_enclosures',
                'cad_rack_devices',
                'cad_branch_circuits',
                'cad_power_protection_devices',
                'cad_poe_budgets',
                'cad_infrastructure_scenarios',
                'cad_rack_thermal_measurements',
                'cad_infrastructure_qualifications',
            )

    # ------------------------------------------------------------------
    # Shared helpers

    def _get_payload(
        self, table: str, column: str, key: str, model
    ):
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} '
                f'WHERE {column}=?',
                (key,),
            ).fetchone()
        if row is None:
            return None
        return model.model_validate_json(row['payload_json'])

    def _list(
        self,
        *,
        table: str,
        model,
        where: str = 'document_id=?',
        params: tuple[object, ...] = (),
        order: str = 'seq ASC',
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
    # Rack enclosures

    def save_rack(self, rack: RackEnclosure) -> None:
        _assert_sealed(rack, 'rack_sha256', 'rack_id')
        existing = self.get_rack(rack.rack_id)
        if existing is not None:
            if existing.rack_sha256 == rack.rack_sha256:
                return
            raise InfrastructureConflictError(
                'rack enclosures are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rack_enclosures (
                    rack_id, rack_sha256, document_id, label,
                    enclosure_kind, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rack.rack_id,
                    rack.rack_sha256,
                    rack.document_id,
                    rack.label,
                    rack.enclosure_kind,
                    rack.declared_at_utc,
                    rack.model_dump_json(),
                ),
            )

    def get_rack(self, rack_id: str) -> RackEnclosure | None:
        return self._get_payload(
            'cad_rack_enclosures', 'rack_id', rack_id, RackEnclosure
        )

    def list_racks(
        self, document_id: str
    ) -> tuple[RackEnclosure, ...]:
        return self._list(
            table='cad_rack_enclosures',
            model=RackEnclosure,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Installed rack devices

    def save_rack_device(self, device: InstalledRackDevice) -> None:
        _assert_sealed(device, 'device_sha256', 'device_id')
        existing = self.get_rack_device(device.device_id)
        if existing is not None:
            if existing.device_sha256 == device.device_sha256:
                return
            raise InfrastructureConflictError(
                'installed rack devices are append-only'
            )
        rack = self.get_rack(device.rack_ref.ref_id)
        if rack is None:
            raise InfrastructureIntegrityError(
                'a rack device must reference a persisted rack enclosure'
            )
        if rack.rack_sha256 != device.rack_ref.ref_sha256:
            raise InfrastructureIntegrityError(
                'rack device rack hash does not match the stored rack'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rack_devices (
                    device_id, device_sha256, document_id, label,
                    rack_ref_id, role, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device.device_id,
                    device.device_sha256,
                    device.document_id,
                    device.label,
                    device.rack_ref.ref_id,
                    device.role,
                    device.declared_at_utc,
                    device.model_dump_json(),
                ),
            )

    def get_rack_device(
        self, device_id: str
    ) -> InstalledRackDevice | None:
        return self._get_payload(
            'cad_rack_devices', 'device_id', device_id, InstalledRackDevice
        )

    def list_rack_devices(
        self, document_id: str
    ) -> tuple[InstalledRackDevice, ...]:
        return self._list(
            table='cad_rack_devices',
            model=InstalledRackDevice,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Branch circuits

    def save_circuit(self, circuit: BranchCircuit) -> None:
        _assert_sealed(circuit, 'circuit_sha256', 'circuit_id')
        existing = self.get_circuit(circuit.circuit_id)
        if existing is not None:
            if existing.circuit_sha256 == circuit.circuit_sha256:
                return
            raise InfrastructureConflictError(
                'branch circuits are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_branch_circuits (
                    circuit_id, circuit_sha256, document_id, label,
                    nominal_voltage_v, breaker_rating_a,
                    continuous_load_policy, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    circuit.circuit_id,
                    circuit.circuit_sha256,
                    circuit.document_id,
                    circuit.label,
                    circuit.nominal_voltage_v,
                    circuit.breaker_rating_a,
                    circuit.continuous_load_policy,
                    circuit.declared_at_utc,
                    circuit.model_dump_json(),
                ),
            )

    def get_circuit(self, circuit_id: str) -> BranchCircuit | None:
        return self._get_payload(
            'cad_branch_circuits', 'circuit_id', circuit_id, BranchCircuit
        )

    def list_circuits(
        self, document_id: str
    ) -> tuple[BranchCircuit, ...]:
        return self._list(
            table='cad_branch_circuits',
            model=BranchCircuit,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Power protection devices

    def save_protection(self, device: PowerProtectionDevice) -> None:
        _assert_sealed(device, 'protection_sha256', 'protection_id')
        existing = self.get_protection(device.protection_id)
        if existing is not None:
            if existing.protection_sha256 == device.protection_sha256:
                return
            raise InfrastructureConflictError(
                'power protection devices are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_power_protection_devices (
                    protection_id, protection_sha256, document_id,
                    label, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    device.protection_id,
                    device.protection_sha256,
                    device.document_id,
                    device.label,
                    device.declared_at_utc,
                    device.model_dump_json(),
                ),
            )

    def get_protection(
        self, protection_id: str
    ) -> PowerProtectionDevice | None:
        return self._get_payload(
            'cad_power_protection_devices', 'protection_id',
            protection_id, PowerProtectionDevice,
        )

    def list_protections(
        self, document_id: str
    ) -> tuple[PowerProtectionDevice, ...]:
        return self._list(
            table='cad_power_protection_devices',
            model=PowerProtectionDevice,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # PoE budgets

    def save_poe_budget(self, budget: PoEBudget) -> None:
        _assert_sealed(budget, 'poe_sha256', 'poe_id')
        existing = self.get_poe_budget(budget.poe_id)
        if existing is not None:
            if existing.poe_sha256 == budget.poe_sha256:
                return
            raise InfrastructureConflictError(
                'PoE budgets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_poe_budgets (
                    poe_id, poe_sha256, document_id, label, standard,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    budget.poe_id,
                    budget.poe_sha256,
                    budget.document_id,
                    budget.label,
                    budget.standard,
                    budget.declared_at_utc,
                    budget.model_dump_json(),
                ),
            )

    def get_poe_budget(self, poe_id: str) -> PoEBudget | None:
        return self._get_payload(
            'cad_poe_budgets', 'poe_id', poe_id, PoEBudget
        )

    def list_poe_budgets(
        self, document_id: str
    ) -> tuple[PoEBudget, ...]:
        return self._list(
            table='cad_poe_budgets',
            model=PoEBudget,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Infrastructure scenarios

    def save_scenario(self, scenario: InfrastructureScenario) -> None:
        _assert_sealed(scenario, 'scenario_sha256', 'scenario_id')
        existing = self.get_scenario(scenario.scenario_id)
        if existing is not None:
            if existing.scenario_sha256 == scenario.scenario_sha256:
                return
            raise InfrastructureConflictError(
                'infrastructure scenarios are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_infrastructure_scenarios (
                    scenario_id, scenario_sha256, document_id, kind,
                    name, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.kind,
                    scenario.name,
                    scenario.declared_at_utc,
                    scenario.model_dump_json(),
                ),
            )

    def get_scenario(
        self, scenario_id: str
    ) -> InfrastructureScenario | None:
        return self._get_payload(
            'cad_infrastructure_scenarios', 'scenario_id',
            scenario_id, InfrastructureScenario,
        )

    def list_scenarios(
        self, document_id: str
    ) -> tuple[InfrastructureScenario, ...]:
        return self._list(
            table='cad_infrastructure_scenarios',
            model=InfrastructureScenario,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Rack thermal measurements

    def save_thermal_measurement(
        self, measurement: RackThermalMeasurement
    ) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_thermal_measurement(
            measurement.measurement_id
        )
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise InfrastructureConflictError(
                'thermal measurements are append-only'
            )
        rack = self.get_rack(measurement.rack_ref.ref_id)
        if rack is None:
            raise InfrastructureIntegrityError(
                'a thermal measurement must reference a persisted rack'
            )
        if rack.rack_sha256 != measurement.rack_ref.ref_sha256:
            raise InfrastructureIntegrityError(
                'thermal measurement rack hash does not match the '
                'stored rack'
            )
        if measurement.scenario_ref is not None:
            scenario = self.get_scenario(measurement.scenario_ref.ref_id)
            if scenario is None:
                raise InfrastructureIntegrityError(
                    'a scenario-bound thermal measurement must '
                    'reference a persisted scenario'
                )
            if scenario.scenario_sha256 != (
                measurement.scenario_ref.ref_sha256
            ):
                raise InfrastructureIntegrityError(
                    'thermal measurement scenario hash does not match '
                    'the stored scenario'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rack_thermal_measurements (
                    measurement_id, measurement_sha256, document_id,
                    rack_ref_id, measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.rack_ref.ref_id,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_thermal_measurement(
        self, measurement_id: str
    ) -> RackThermalMeasurement | None:
        return self._get_payload(
            'cad_rack_thermal_measurements', 'measurement_id',
            measurement_id, RackThermalMeasurement,
        )

    def list_thermal_measurements(
        self, document_id: str
    ) -> tuple[RackThermalMeasurement, ...]:
        return self._list(
            table='cad_rack_thermal_measurements',
            model=RackThermalMeasurement,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Qualification verdicts

    def save_qualification(
        self, qualification: InfrastructureQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == (
                qualification.qualification_sha256
            ):
                return
            raise InfrastructureConflictError(
                'infrastructure qualifications are append-only'
            )
        scenario = self.get_scenario(
            qualification.scenario_ref.ref_id
        )
        if scenario is None:
            raise InfrastructureIntegrityError(
                'a qualification must reference a persisted scenario'
            )
        if scenario.scenario_sha256 != (
            qualification.scenario_ref.ref_sha256
        ):
            raise InfrastructureIntegrityError(
                'qualification scenario hash does not match the stored '
                'scenario'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_infrastructure_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    scenario_ref_id, overall_state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.scenario_ref.ref_id,
                    qualification.overall_state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> InfrastructureQualification | None:
        return self._get_payload(
            'cad_infrastructure_qualifications', 'qualification_id',
            qualification_id, InfrastructureQualification,
        )

    def list_qualifications(
        self, document_id: str
    ) -> tuple[InfrastructureQualification, ...]:
        return self._list(
            table='cad_infrastructure_qualifications',
            model=InfrastructureQualification,
            params=(document_id,),
        )
