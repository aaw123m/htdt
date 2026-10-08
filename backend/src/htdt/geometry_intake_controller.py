"""Geometry intake controller (#866 REV70).

Non-Qt owner of the intake chain for one document:

    IFC file -> artifact + entity mappings -> GeometryIntakeSubject ->
    HEALTH_CHECK -> REPAIR_PROPOSAL -> operator decisions ->
    EXPLICIT_ACCEPTANCE -> DERIVED_GEOMETRY_REVISION -> SOLVER_READINESS

Every sealed step is persisted through ``CadGeometryIntakeRepository`` /
``CadIfcInteropRepository``; the controller only keeps the *current* chain
position in memory so the Qt panel can render it. Decisions accumulate in
``pending_decisions`` until every proposed action is covered — the
acceptance record is sealed only on exact cover.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .cad_acoustic_solver_adapter import AcousticSolverAdapterDescriptor
from .cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from .cad_geometry_intake import (
    DerivedGeometryRevision,
    GeometryIntakeError,
    GeometryIntakeReport,
    GeometryIntakeSubject,
    GeometryRepairAcceptance,
    GeometryRepairProposal,
    GeometrySolverReadinessVerdict,
    ReadinessEvidenceState,
    RepairActionDecision,
    RepairDecisionValue,
    SolverGeometryContext,
    build_geometry_intake_subject,
    build_ifc_intake_subject,
    derive_geometry_revision,
    derive_solver_geometry_context,
    diagnose_geometry_intake,
    evaluate_solver_readiness,
    propose_geometry_repairs,
    readiness_evidence_state,
    record_geometry_repair_acceptance,
)
from .cad_geometry_intake_repository import CadGeometryIntakeRepository
from .cad_ifc_interop import IfcImportArtifact, build_ifc_import
from .cad_ifc_repository import CadIfcInteropRepository
from .cad_repository import SceneRepository
from .cad_solver_capability_manifest import SolverCapabilityManifest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GeometryIntakeController:
    """Owns the intake chain state + persistence for one document."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        operator_id: str = 'operator',
        now_utc: Callable[[], str] | None = None,
    ) -> None:
        self.repository = scene_repository
        self.document_id = document_id
        self.operator_id = operator_id
        self._now = now_utc or _utc_now
        self.intake_repository = CadGeometryIntakeRepository(scene_repository)
        self.ifc_repository = CadIfcInteropRepository(scene_repository)
        self.dispatch_repository = dispatch_repository
        self.artifact: IfcImportArtifact | None = None
        self.subject: GeometryIntakeSubject | None = None
        self.report: GeometryIntakeReport | None = None
        self.proposal: GeometryRepairProposal | None = None
        self.acceptance: GeometryRepairAcceptance | None = None
        self.revision: DerivedGeometryRevision | None = None
        self.verdict: GeometrySolverReadinessVerdict | None = None
        self.solver_descriptor: AcousticSolverAdapterDescriptor | None = None
        self.solver_manifest: SolverCapabilityManifest | None = None
        self.pending_decisions: dict[str, RepairActionDecision] = {}

    # --- solver selection ------------------------------------------------

    def solver_options(
        self,
    ) -> tuple[
        tuple[AcousticSolverAdapterDescriptor, SolverCapabilityManifest], ...
    ]:
        """Registered solver adapters (+ their declared manifests)."""
        options: list[
            tuple[AcousticSolverAdapterDescriptor, SolverCapabilityManifest]
        ] = []
        for manifest in self.dispatch_repository.list_capability_manifests():
            descriptor = self.dispatch_repository.get_descriptor(
                manifest.adapter_descriptor_id
            )
            if descriptor is not None:
                options.append((descriptor, manifest))
        return tuple(options)

    def select_solver(
        self,
        descriptor: AcousticSolverAdapterDescriptor,
        manifest: SolverCapabilityManifest | None = None,
    ) -> GeometrySolverReadinessVerdict | None:
        """Bind the target solver; re-evaluates readiness when a subject
        has already been diagnosed (stale-solver semantics make a silent
        carry-over dishonest)."""
        self.solver_descriptor = descriptor
        self.solver_manifest = manifest
        if self.subject is None or self.report is None:
            return None
        return self.evaluate_readiness()

    def _solver_context(self) -> SolverGeometryContext | None:
        if self.solver_descriptor is None:
            return None
        return derive_solver_geometry_context(
            self.solver_descriptor, self.solver_manifest
        )

    # --- source intake ---------------------------------------------------

    def import_ifc_source(
        self,
        source: bytes | str,
        *,
        file_name: str,
    ) -> tuple[IfcImportArtifact, GeometryIntakeSubject]:
        """Parse + seal a real IFC file and build the intake subject."""
        artifact, mappings = build_ifc_import(
            document_id=self.document_id,
            file_name=file_name,
            source=source,
            imported_at_utc=self._now(),
        )
        self.ifc_repository.save_import_artifact(artifact)
        self.ifc_repository.save_entity_mappings(mappings)
        subject = build_ifc_intake_subject(
            self.document_id, artifact, mappings
        )
        self._reset_chain(artifact=artifact, subject=subject)
        return artifact, subject

    def adopt_current_subject(
        self,
        *,
        source_revision_ref=None,
        read_blob=None,
    ) -> GeometryIntakeSubject:
        """Build the intake subject from the live scene document."""
        revision = self.repository.latest(self.document_id)
        if revision is None:
            raise GeometryIntakeError(
                f'no committed scene revision for {self.document_id}'
            )
        subject = build_geometry_intake_subject(
            revision.document,
            source_kind='scene_document',
            source_revision_ref=source_revision_ref,
            read_blob=read_blob,
        )
        self._reset_chain(artifact=None, subject=subject)
        return subject

    def _reset_chain(
        self,
        *,
        artifact: IfcImportArtifact | None,
        subject: GeometryIntakeSubject,
    ) -> None:
        self.artifact = artifact
        self.subject = subject
        self.report = None
        self.proposal = None
        self.acceptance = None
        self.revision = None
        # The prior verdict is deliberately retained: evidence-state
        # evaluation marks it stale against the new geometry instead of
        # silently dropping a bound result.
        self.pending_decisions.clear()

    # --- health check ----------------------------------------------------

    def run_health_check(
        self,
        *,
        solver_context: SolverGeometryContext | None = None,
    ) -> tuple[GeometryIntakeReport, GeometryRepairProposal]:
        """HEALTH_CHECK + REPAIR_PROPOSAL, both persisted."""
        if self.subject is None:
            raise GeometryIntakeError('no intake subject — import first')
        context = solver_context or self._solver_context()
        report = diagnose_geometry_intake(
            self.subject,
            evaluated_at_utc=self._now(),
            solver_context=context,
        )
        self.intake_repository.save_intake_report(report)
        proposal = propose_geometry_repairs(
            report,
            self.subject,
            proposed_by=self.operator_id,
            proposed_at_utc=self._now(),
            proposal_reason='operator-initiated health check',
        )
        self.intake_repository.save_repair_proposal(proposal)
        self.report = report
        self.proposal = proposal
        self.acceptance = None
        self.revision = None
        self.pending_decisions.clear()
        return report, proposal

    # --- decisions -> acceptance -----------------------------------------

    def undecided_action_ids(self) -> tuple[str, ...]:
        if self.proposal is None:
            return ()
        decided = set(self.pending_decisions)
        return tuple(
            action.action_id
            for action in self.proposal.actions
            if action.action_id not in decided
        )

    def submit_decision(
        self,
        action_id: str,
        decision: RepairDecisionValue,
        *,
        accepted_parameters: dict[str, Any] | None = None,
        decision_reason: str | None = None,
    ) -> GeometryRepairAcceptance | None:
        """Record one operator decision; seals the acceptance record once
        the last undecided action is covered (exact-cover semantics)."""
        if self.proposal is None:
            raise GeometryIntakeError('no repair proposal to decide')
        known = {a.action_id for a in self.proposal.actions}
        if action_id not in known:
            raise GeometryIntakeError(
                f'decision targets action outside the proposal: {action_id}'
            )
        self.pending_decisions[action_id] = RepairActionDecision(
            action_id=action_id,
            decision=decision,
            decided_by=self.operator_id,
            decided_at_utc=self._now(),
            accepted_parameters=accepted_parameters or {},
            decision_reason=decision_reason,
        )
        if self.undecided_action_ids():
            return None
        acceptance = record_geometry_repair_acceptance(
            self.proposal,
            tuple(self.pending_decisions.values()),
            decided_by=self.operator_id,
            decided_at_utc=self._now(),
        )
        self.intake_repository.save_repair_acceptance(acceptance)
        self.acceptance = acceptance
        return acceptance

    # --- derived revision -------------------------------------------------

    def derive_revision(
        self,
        *,
        solver_context: SolverGeometryContext | None = None,
    ) -> DerivedGeometryRevision:
        """DERIVED_GEOMETRY_REVISION from the sealed acceptance."""
        if self.acceptance is None:
            raise GeometryIntakeError(
                'derived revision requires a completed acceptance'
            )
        revision = derive_geometry_revision(
            self.subject,
            self.report,
            self.proposal,
            self.acceptance,
            derived_by=self.operator_id,
            derived_at_utc=self._now(),
            solver_context=solver_context or self._solver_context(),
        )
        self.intake_repository.save_derived_revision(revision)
        self.revision = revision
        return revision

    # --- readiness ---------------------------------------------------------

    def evaluate_readiness(
        self,
        *,
        adapter_descriptor: AcousticSolverAdapterDescriptor | None = None,
        capability_manifest: SolverCapabilityManifest | None = None,
        solver_context: SolverGeometryContext | None = None,
    ) -> GeometrySolverReadinessVerdict:
        """SOLVER_READINESS for the derived revision when present, else the
        source subject — persisted so evidence invalidation stays
        deterministic."""
        descriptor = adapter_descriptor or self.solver_descriptor
        manifest = (
            capability_manifest
            if capability_manifest is not None
            else self.solver_manifest
        )
        if descriptor is None:
            raise GeometryIntakeError(
                'readiness evaluation requires a selected solver adapter'
            )
        if self.report is None or self.subject is None:
            raise GeometryIntakeError('no diagnosed subject to evaluate')
        if self.revision is not None:
            verdict = evaluate_solver_readiness(
                derived_revision=self.revision,
                report=self.report,
                adapter_descriptor=descriptor,
                capability_manifest=manifest,
                solver_context=solver_context,
                evaluated_at_utc=self._now(),
            )
        else:
            verdict = evaluate_solver_readiness(
                subject=self.subject,
                report=self.report,
                adapter_descriptor=descriptor,
                capability_manifest=manifest,
                solver_context=solver_context,
                evaluated_at_utc=self._now(),
            )
        self.intake_repository.save_solver_readiness(verdict)
        self.verdict = verdict
        return verdict

    def verdict_evidence_state(self) -> ReadinessEvidenceState | None:
        """Freshness of the current verdict against live geometry+manifest."""
        if self.verdict is None or self.subject is None:
            return None
        geometry_sha = (
            self.revision.derived_geometry_sha256
            if self.revision is not None
            else self.subject.subject_sha256
        )
        manifest_sha = (
            self.solver_manifest.semantic_sha256
            if self.solver_manifest is not None
            else None
        )
        return readiness_evidence_state(
            self.verdict,
            current_geometry_sha256=geometry_sha,
            current_adapter_sha256=(
                self.solver_descriptor.semantic_sha256
                if self.solver_descriptor is not None
                else None
            ),
            current_manifest_sha256=manifest_sha,
        )


__all__ = [
    'GeometryIntakeController',
]
