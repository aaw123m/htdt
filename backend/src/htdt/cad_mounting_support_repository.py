"""Append-only persistence for the AV mounting / structural-support
authority (#620, REV57-MOUNT).

Seven tables:

* ``cad_mount_assemblies`` — sealed equipment→mount→hardware→support
  assembly identities.
* ``cad_mount_load_evidence`` — sealed demand evidence (mass/CG/duty).
* ``cad_mount_support_elements`` — sealed load-carrying element
  declarations (never capacity derivations).
* ``cad_mount_manufacturer_requirements`` — sealed manufacturer mounting
  instructions.
* ``cad_mount_structural_approvals`` — sealed approval artifacts
  (engineer / professional / permit / standard-profile / installer /
  inspection / proof-test classes).
* ``cad_mount_inspection_records`` — sealed installation/periodic
  inspection as-builts.
* ``cad_mount_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_mounting_support_authority import (
    CadManufacturerMountingRequirement,
    CadMountingAssembly,
    CadMountingInspectionRecord,
    CadMountingQualification,
    CadMountLoadEvidence,
    CadStructuralApprovalRecord,
    CadSupportElementRecord,
)


class MountingSupportConflictError(ValueError):
    """A mounting-support save violated append-only identity rules."""


class MountingSupportIntegrityError(ValueError):
    """A stored mounting-support row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise MountingSupportIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MountingSupportIntegrityError(
            'record id does not match its sealed sha256'
        )


def _column(connection: sqlite3.Connection, row: sqlite3.Row, name: str):
    return row[name]


class CadMountingSupportRepository:
    """Native storage for the #620 mounting-support authority."""

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
                'cad_mount_assemblies',
                'cad_mount_load_evidence',
                'cad_mount_support_elements',
                'cad_mount_manufacturer_requirements',
                'cad_mount_structural_approvals',
                'cad_mount_inspection_records',
                'cad_mount_qualifications',
            )

    # ------------------------------------------------------------------
    # Assemblies

    def save_assembly(self, assembly: CadMountingAssembly) -> None:
        _assert_sealed(assembly, 'assembly_sha256', 'assembly_id')
        existing = self.get_assembly(assembly.assembly_id)
        if existing is not None:
            if existing.assembly_sha256 == assembly.assembly_sha256:
                return
            raise MountingSupportConflictError(
                'mounting assemblies are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_assemblies (
                    assembly_id, assembly_sha256, document_id,
                    equipment_ref_id, placement_ref_id, equipment_class,
                    support_method, overhead_suspension, duty_state,
                    interference_state, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assembly.assembly_id,
                    assembly.assembly_sha256,
                    assembly.document_id,
                    (
                        assembly.equipment_ref.ref_id
                        if assembly.equipment_ref is not None
                        else None
                    ),
                    (
                        assembly.placement_ref.ref_id
                        if assembly.placement_ref is not None
                        else None
                    ),
                    assembly.equipment_class,
                    assembly.support_method,
                    1 if assembly.overhead_suspension else 0,
                    assembly.duty_state,
                    assembly.interference_state,
                    assembly.declared_at_utc,
                    assembly.model_dump_json(),
                ),
            )

    def get_assembly(
        self, assembly_id: str
    ) -> CadMountingAssembly | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_assemblies WHERE assembly_id=?',
                (assembly_id,),
            ).fetchone()
        if row is None:
            return None
        assembly = CadMountingAssembly.model_validate_json(
            row['payload_json']
        )
        if (
            assembly.assembly_id != row['assembly_id']
            or assembly.assembly_sha256 != row['assembly_sha256']
            or assembly.document_id != row['document_id']
            or (
                assembly.equipment_ref.ref_id
                if assembly.equipment_ref is not None
                else None
            ) != row['equipment_ref_id']
            or (
                assembly.placement_ref.ref_id
                if assembly.placement_ref is not None
                else None
            ) != row['placement_ref_id']
            or assembly.equipment_class != row['equipment_class']
            or assembly.support_method != row['support_method']
            or (1 if assembly.overhead_suspension else 0)
            != row['overhead_suspension']
            or assembly.duty_state != row['duty_state']
            or assembly.interference_state != row['interference_state']
            or assembly.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored mounting assembly row disagrees with its payload'
            )
        return assembly

    def list_assemblies(
        self, document_id: str | None = None
    ) -> tuple[CadMountingAssembly, ...]:
        query = 'SELECT payload_json FROM cad_mount_assemblies'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMountingAssembly.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Load evidence

    def save_load_evidence(
        self, evidence: CadMountLoadEvidence
    ) -> None:
        _assert_sealed(evidence, 'evidence_sha256', 'evidence_id')
        existing = self.get_load_evidence(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise MountingSupportConflictError(
                'mount load evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_load_evidence (
                    evidence_id, evidence_sha256, document_id,
                    assembly_ref_id, mass_kg, weight_n, duty_state,
                    source_class, measured_at_utc, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    evidence.assembly_ref.ref_id,
                    evidence.mass_kg,
                    evidence.weight_n,
                    evidence.duty_state,
                    evidence.source_class,
                    evidence.measured_at_utc,
                    evidence.declared_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_load_evidence(
        self, evidence_id: str
    ) -> CadMountLoadEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_load_evidence WHERE '
                'evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = CadMountLoadEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or evidence.assembly_ref.ref_id != row['assembly_ref_id']
            or evidence.mass_kg != row['mass_kg']
            or evidence.weight_n != row['weight_n']
            or evidence.duty_state != row['duty_state']
            or evidence.source_class != row['source_class']
            or evidence.measured_at_utc != row['measured_at_utc']
            or evidence.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored mount load evidence disagrees with its payload'
            )
        return evidence

    def list_load_evidence(
        self, document_id: str | None = None
    ) -> tuple[CadMountLoadEvidence, ...]:
        query = 'SELECT payload_json FROM cad_mount_load_evidence'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMountLoadEvidence.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Support elements

    def save_support_element(
        self, element: CadSupportElementRecord
    ) -> None:
        _assert_sealed(element, 'element_sha256', 'element_id')
        existing = self.get_support_element(element.element_id)
        if existing is not None:
            if existing.element_sha256 == element.element_sha256:
                return
            raise MountingSupportConflictError(
                'support elements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_support_elements (
                    element_id, element_sha256, document_id,
                    assembly_ref_id, element_class, geometry_ref_id,
                    hidden_condition_state, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    element.element_id,
                    element.element_sha256,
                    element.document_id,
                    element.assembly_ref.ref_id,
                    element.element_class,
                    (
                        element.geometry_ref.ref_id
                        if element.geometry_ref is not None
                        else None
                    ),
                    element.hidden_condition_state,
                    element.declared_at_utc,
                    element.model_dump_json(),
                ),
            )

    def get_support_element(
        self, element_id: str
    ) -> CadSupportElementRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_support_elements WHERE '
                'element_id=?',
                (element_id,),
            ).fetchone()
        if row is None:
            return None
        element = CadSupportElementRecord.model_validate_json(
            row['payload_json']
        )
        if (
            element.element_id != row['element_id']
            or element.element_sha256 != row['element_sha256']
            or element.document_id != row['document_id']
            or element.assembly_ref.ref_id != row['assembly_ref_id']
            or element.element_class != row['element_class']
            or (
                element.geometry_ref.ref_id
                if element.geometry_ref is not None
                else None
            ) != row['geometry_ref_id']
            or element.hidden_condition_state
            != row['hidden_condition_state']
            or element.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored support element row disagrees with its payload'
            )
        return element

    def list_support_elements(
        self, document_id: str | None = None
    ) -> tuple[CadSupportElementRecord, ...]:
        query = 'SELECT payload_json FROM cad_mount_support_elements'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadSupportElementRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Manufacturer requirements

    def save_manufacturer_requirement(
        self, requirement: CadManufacturerMountingRequirement
    ) -> None:
        _assert_sealed(
            requirement, 'requirement_sha256', 'requirement_id'
        )
        existing = self.get_manufacturer_requirement(
            requirement.requirement_id
        )
        if existing is not None:
            if existing.requirement_sha256 == (
                requirement.requirement_sha256
            ):
                return
            raise MountingSupportConflictError(
                'manufacturer mounting requirements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_manufacturer_requirements (
                    requirement_id, requirement_sha256, document_id,
                    subject_ref_id, secondary_retention,
                    enclosure_suspension, vesa_pattern, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement.requirement_id,
                    requirement.requirement_sha256,
                    requirement.document_id,
                    requirement.subject_ref.ref_id,
                    requirement.secondary_retention,
                    requirement.enclosure_suspension,
                    requirement.vesa_pattern,
                    requirement.declared_at_utc,
                    requirement.model_dump_json(),
                ),
            )

    def get_manufacturer_requirement(
        self, requirement_id: str
    ) -> CadManufacturerMountingRequirement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_manufacturer_requirements WHERE '
                'requirement_id=?',
                (requirement_id,),
            ).fetchone()
        if row is None:
            return None
        requirement = (
            CadManufacturerMountingRequirement.model_validate_json(
                row['payload_json']
            )
        )
        if (
            requirement.requirement_id != row['requirement_id']
            or requirement.requirement_sha256 != row['requirement_sha256']
            or requirement.document_id != row['document_id']
            or requirement.subject_ref.ref_id != row['subject_ref_id']
            or requirement.secondary_retention
            != row['secondary_retention']
            or requirement.enclosure_suspension
            != row['enclosure_suspension']
            or requirement.vesa_pattern != row['vesa_pattern']
            or requirement.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored manufacturer requirement disagrees with its '
                'payload'
            )
        return requirement

    def list_manufacturer_requirements(
        self, document_id: str | None = None
    ) -> tuple[CadManufacturerMountingRequirement, ...]:
        query = (
            'SELECT payload_json FROM cad_mount_manufacturer_requirements'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadManufacturerMountingRequirement.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Structural approvals

    def save_approval(
        self, approval: CadStructuralApprovalRecord
    ) -> None:
        _assert_sealed(approval, 'approval_sha256', 'approval_id')
        existing = self.get_approval(approval.approval_id)
        if existing is not None:
            if existing.approval_sha256 == approval.approval_sha256:
                return
            raise MountingSupportConflictError(
                'structural approvals are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_structural_approvals (
                    approval_id, approval_sha256, document_id,
                    assembly_ref_id, evidence_class, approval_scope,
                    duty_coverage, standard_ref_id, jurisdiction,
                    issued_at_utc, expires_at_utc, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.approval_sha256,
                    approval.document_id,
                    (
                        approval.assembly_ref.ref_id
                        if approval.assembly_ref is not None
                        else None
                    ),
                    approval.evidence_class,
                    approval.approval_scope,
                    approval.duty_coverage,
                    (
                        approval.standard_ref.ref_id
                        if approval.standard_ref is not None
                        else None
                    ),
                    approval.jurisdiction,
                    approval.issued_at_utc,
                    approval.expires_at_utc,
                    approval.declared_at_utc,
                    approval.model_dump_json(),
                ),
            )

    def get_approval(
        self, approval_id: str
    ) -> CadStructuralApprovalRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_structural_approvals WHERE '
                'approval_id=?',
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        approval = CadStructuralApprovalRecord.model_validate_json(
            row['payload_json']
        )
        if (
            approval.approval_id != row['approval_id']
            or approval.approval_sha256 != row['approval_sha256']
            or approval.document_id != row['document_id']
            or (
                approval.assembly_ref.ref_id
                if approval.assembly_ref is not None
                else None
            ) != row['assembly_ref_id']
            or approval.evidence_class != row['evidence_class']
            or approval.approval_scope != row['approval_scope']
            or approval.duty_coverage != row['duty_coverage']
            or (
                approval.standard_ref.ref_id
                if approval.standard_ref is not None
                else None
            ) != row['standard_ref_id']
            or approval.jurisdiction != row['jurisdiction']
            or approval.issued_at_utc != row['issued_at_utc']
            or approval.expires_at_utc != row['expires_at_utc']
            or approval.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored structural approval row disagrees with its '
                'payload'
            )
        return approval

    def list_approvals(
        self, document_id: str | None = None
    ) -> tuple[CadStructuralApprovalRecord, ...]:
        query = (
            'SELECT payload_json FROM cad_mount_structural_approvals'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadStructuralApprovalRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Inspection records

    def save_inspection(
        self, inspection: CadMountingInspectionRecord
    ) -> None:
        _assert_sealed(
            inspection, 'inspection_sha256', 'inspection_id'
        )
        existing = self.get_inspection(inspection.inspection_id)
        if existing is not None:
            if existing.inspection_sha256 == inspection.inspection_sha256:
                return
            raise MountingSupportConflictError(
                'mounting inspections are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_inspection_records (
                    inspection_id, inspection_sha256, document_id,
                    assembly_ref_id, inspection_kind, inspector_class,
                    findings, inspected_at_utc, next_due_at_utc,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inspection.inspection_id,
                    inspection.inspection_sha256,
                    inspection.document_id,
                    inspection.assembly_ref.ref_id,
                    inspection.inspection_kind,
                    inspection.inspector_class,
                    inspection.findings,
                    inspection.inspected_at_utc,
                    inspection.next_due_at_utc,
                    inspection.declared_at_utc,
                    inspection.model_dump_json(),
                ),
            )

    def get_inspection(
        self, inspection_id: str
    ) -> CadMountingInspectionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_inspection_records WHERE '
                'inspection_id=?',
                (inspection_id,),
            ).fetchone()
        if row is None:
            return None
        inspection = CadMountingInspectionRecord.model_validate_json(
            row['payload_json']
        )
        if (
            inspection.inspection_id != row['inspection_id']
            or inspection.inspection_sha256 != row['inspection_sha256']
            or inspection.document_id != row['document_id']
            or inspection.assembly_ref.ref_id != row['assembly_ref_id']
            or inspection.inspection_kind != row['inspection_kind']
            or inspection.inspector_class != row['inspector_class']
            or inspection.findings != row['findings']
            or inspection.inspected_at_utc != row['inspected_at_utc']
            or inspection.next_due_at_utc != row['next_due_at_utc']
            or inspection.declared_at_utc != row['declared_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored mounting inspection disagrees with its payload'
            )
        return inspection

    def list_inspections(
        self, document_id: str | None = None
    ) -> tuple[CadMountingInspectionRecord, ...]:
        query = 'SELECT payload_json FROM cad_mount_inspection_records'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMountingInspectionRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadMountingQualification
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
            raise MountingSupportConflictError(
                'mounting qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mount_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    assembly_ref_id, support_state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.assembly_ref.ref_id,
                    qualification.support_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadMountingQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mount_qualifications WHERE '
                'qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadMountingQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.assembly_ref.ref_id
            != row['assembly_ref_id']
            or qualification.support_state != row['support_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc
            != row['evaluated_at_utc']
        ):
            raise MountingSupportIntegrityError(
                'stored mounting qualification disagrees with its '
                'payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[CadMountingQualification, ...]:
        query = 'SELECT payload_json FROM cad_mount_qualifications'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMountingQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
