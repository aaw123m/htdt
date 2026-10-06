"""Append-only persistence for the swept-sine deconvolution /
harmonic-separation authority (#697, REV58-MEASCHAIN).

Four tables:

* ``cad_sweep_deconvolution_specs`` — sealed deconvolution
  specifications.
* ``cad_harmonic_impulse_components`` — sealed per-order harmonic
  components.
* ``cad_recovered_impulse_responses`` — sealed recovered-IR records.
* ``cad_linear_ir_capabilities`` — sealed fail-closed per-metric
  capability verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_sweep_deconvolution import (
    CadHarmonicImpulseComponent,
    CadLinearIRCapability,
    CadRecoveredImpulseResponse,
    CadSweepDeconvolutionSpec,
)


class SweepDeconvAuthorityConflictError(ValueError):
    """A deconvolution-authority save violated append-only rules."""


class SweepDeconvAuthorityIntegrityError(ValueError):
    """A stored deconvolution row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SweepDeconvAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SweepDeconvAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSweepDeconvolutionRepository:
    """Native storage for the #697 deconvolution-authority records."""

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
                'cad_sweep_deconvolution_specs',
                'cad_harmonic_impulse_components',
                'cad_recovered_impulse_responses',
                'cad_linear_ir_capabilities',
            )

    # ------------------------------------------------------------------
    # Deconvolution specs

    def save_spec(self, spec: CadSweepDeconvolutionSpec) -> None:
        _assert_sealed(spec, 'spec_sha256', 'spec_id')
        existing = self.get_spec(spec.spec_id)
        if existing is not None:
            if existing.spec_sha256 == spec.spec_sha256:
                return
            raise SweepDeconvAuthorityConflictError(
                'deconvolution specs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_sweep_deconvolution_specs (
                    spec_id, spec_sha256, document_id,
                    stimulus_ref_id, sweep_law, algorithm,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.spec_sha256,
                    spec.document_id,
                    spec.stimulus_ref.ref_id,
                    spec.sweep_law,
                    spec.algorithm,
                    spec.declared_at_utc,
                    spec.model_dump_json(),
                ),
            )

    def get_spec(
        self, spec_id: str
    ) -> CadSweepDeconvolutionSpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_sweep_deconvolution_specs '
                'WHERE spec_id=?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        spec = CadSweepDeconvolutionSpec.model_validate_json(
            row['payload_json']
        )
        if (
            spec.spec_id != row['spec_id']
            or spec.spec_sha256 != row['spec_sha256']
            or spec.document_id != row['document_id']
            or spec.stimulus_ref.ref_id != row['stimulus_ref_id']
            or spec.sweep_law != row['sweep_law']
            or spec.algorithm != row['algorithm']
            or spec.declared_at_utc != row['declared_at_utc']
        ):
            raise SweepDeconvAuthorityIntegrityError(
                'deconvolution spec row disagrees with payload'
            )
        return spec

    def list_specs(
        self, document_id: str
    ) -> tuple[CadSweepDeconvolutionSpec, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_sweep_deconvolution_specs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSweepDeconvolutionSpec.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Harmonic impulse components

    def save_component(
        self, component: CadHarmonicImpulseComponent
    ) -> None:
        _assert_sealed(component, 'component_sha256', 'component_id')
        existing = self.get_component(component.component_id)
        if existing is not None:
            if existing.component_sha256 == component.component_sha256:
                return
            raise SweepDeconvAuthorityConflictError(
                'harmonic components are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_harmonic_impulse_components (
                    component_id, component_sha256, document_id,
                    spec_ref_id, harmonic_order, expected_offset_s,
                    overlap_state, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    component.component_id,
                    component.component_sha256,
                    component.document_id,
                    component.spec_ref.ref_id,
                    component.harmonic_order,
                    component.expected_offset_s,
                    component.overlap_state,
                    component.declared_at_utc,
                    component.model_dump_json(),
                ),
            )

    def get_component(
        self, component_id: str
    ) -> CadHarmonicImpulseComponent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_harmonic_impulse_components '
                'WHERE component_id=?',
                (component_id,),
            ).fetchone()
        if row is None:
            return None
        component = CadHarmonicImpulseComponent.model_validate_json(
            row['payload_json']
        )
        if (
            component.component_id != row['component_id']
            or component.component_sha256 != row['component_sha256']
            or component.document_id != row['document_id']
            or component.spec_ref.ref_id != row['spec_ref_id']
            or component.harmonic_order != row['harmonic_order']
            or component.expected_offset_s != row['expected_offset_s']
            or component.overlap_state != row['overlap_state']
            or component.declared_at_utc != row['declared_at_utc']
        ):
            raise SweepDeconvAuthorityIntegrityError(
                'harmonic component row disagrees with payload'
            )
        return component

    def list_components(
        self, document_id: str
    ) -> tuple[CadHarmonicImpulseComponent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_harmonic_impulse_components '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadHarmonicImpulseComponent.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Recovered impulse responses

    def save_recovered_ir(self, ir: CadRecoveredImpulseResponse) -> None:
        _assert_sealed(ir, 'ir_sha256', 'ir_id')
        existing = self.get_recovered_ir(ir.ir_id)
        if existing is not None:
            if existing.ir_sha256 == ir.ir_sha256:
                return
            raise SweepDeconvAuthorityConflictError(
                'recovered impulse responses are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_recovered_impulse_responses (
                    ir_id, ir_sha256, document_id, provenance_class,
                    spec_ref_id, raw_capture_ref_id,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ir.ir_id,
                    ir.ir_sha256,
                    ir.document_id,
                    ir.provenance_class,
                    (
                        ir.spec_ref.ref_id
                        if ir.spec_ref is not None
                        else None
                    ),
                    (
                        ir.raw_capture_ref.ref_id
                        if ir.raw_capture_ref is not None
                        else None
                    ),
                    ir.declared_at_utc,
                    ir.model_dump_json(),
                ),
            )

    def get_recovered_ir(
        self, ir_id: str
    ) -> CadRecoveredImpulseResponse | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_recovered_impulse_responses '
                'WHERE ir_id=?',
                (ir_id,),
            ).fetchone()
        if row is None:
            return None
        ir = CadRecoveredImpulseResponse.model_validate_json(
            row['payload_json']
        )
        if (
            ir.ir_id != row['ir_id']
            or ir.ir_sha256 != row['ir_sha256']
            or ir.document_id != row['document_id']
            or ir.provenance_class != row['provenance_class']
            or (ir.spec_ref.ref_id if ir.spec_ref else None)
            != row['spec_ref_id']
            or (ir.raw_capture_ref.ref_id if ir.raw_capture_ref else None)
            != row['raw_capture_ref_id']
            or ir.declared_at_utc != row['declared_at_utc']
        ):
            raise SweepDeconvAuthorityIntegrityError(
                'recovered IR row disagrees with payload'
            )
        return ir

    def list_recovered_irs(
        self, document_id: str
    ) -> tuple[CadRecoveredImpulseResponse, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_recovered_impulse_responses '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadRecoveredImpulseResponse.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Linear-IR capabilities

    def save_capability(
        self, capability: CadLinearIRCapability
    ) -> None:
        _assert_sealed(capability, 'capability_sha256', 'capability_id')
        existing = self.get_capability(capability.capability_id)
        if existing is not None:
            if existing.capability_sha256 == capability.capability_sha256:
                return
            raise SweepDeconvAuthorityConflictError(
                'linear-IR capabilities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_linear_ir_capabilities (
                    capability_id, capability_sha256, document_id,
                    ir_ref_id, contamination_state, clock_gate,
                    chain_gate, evaluation_version, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.capability_sha256,
                    capability.document_id,
                    capability.ir_ref.ref_id,
                    capability.contamination_state,
                    capability.clock_gate,
                    capability.chain_gate,
                    capability.evaluation_version,
                    capability.evaluated_at_utc,
                    capability.model_dump_json(),
                ),
            )

    def get_capability(
        self, capability_id: str
    ) -> CadLinearIRCapability | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_linear_ir_capabilities '
                'WHERE capability_id=?',
                (capability_id,),
            ).fetchone()
        if row is None:
            return None
        capability = CadLinearIRCapability.model_validate_json(
            row['payload_json']
        )
        if (
            capability.capability_id != row['capability_id']
            or capability.capability_sha256 != row['capability_sha256']
            or capability.document_id != row['document_id']
            or capability.ir_ref.ref_id != row['ir_ref_id']
            or capability.contamination_state != row['contamination_state']
            or capability.clock_gate != row['clock_gate']
            or capability.chain_gate != row['chain_gate']
            or capability.evaluation_version != row['evaluation_version']
            or capability.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SweepDeconvAuthorityIntegrityError(
                'linear-IR capability row disagrees with payload'
            )
        return capability

    def list_capabilities(
        self, document_id: str
    ) -> tuple[CadLinearIRCapability, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_linear_ir_capabilities '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLinearIRCapability.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadSweepDeconvolutionRepository',
    'SweepDeconvAuthorityConflictError',
    'SweepDeconvAuthorityIntegrityError',
]
