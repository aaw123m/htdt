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
    build_ifc_diff_subject,
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
from .cad_ifc_diff import (
    IfcDiffApply,
    IfcDiffDecision,
    IfcDiffRow,
    IfcDiffRowDecision,
    build_ifc_diff_apply,
    compute_ifc_diff_rows,
    latest_entity_mappings,
    merge_ifc_diff_mappings,
    reconciliation_marks,
)
from .cad_ifc_interop import (
    IfcEntityMapping,
    IfcImportArtifact,
    IfcRevisionDelta,
    build_ifc_import,
    compute_ifc_revision_delta,
    mark_mapping_reconciliation,
)
from .cad_ifc_repository import CadIfcInteropRepository
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite
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
        # IFC diff-review state (#981). ``diff_base_*`` is the mapping set
        # the current subject was built from — after a plain import it is
        # that import's own set; after a diff apply it is the merged set
        # so the next revision diffs against what the subject actually is.
        self.diff_base_artifact: IfcImportArtifact | None = None
        self.diff_base_mappings: tuple[IfcEntityMapping, ...] | None = None
        self.diff_new_artifact: IfcImportArtifact | None = None
        self.diff_new_mappings: tuple[IfcEntityMapping, ...] | None = None
        self.diff_delta: IfcRevisionDelta | None = None
        self.diff_rows: tuple[IfcDiffRow, ...] = ()
        self.diff_decisions: dict[str, IfcDiffRowDecision] = {}
        self.diff_apply: IfcDiffApply | None = None

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
        self.diff_base_artifact = artifact
        self.diff_base_mappings = tuple(mappings)
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
        self.diff_base_artifact = None
        self.diff_base_mappings = None
        self.diff_new_artifact = None
        self.diff_new_mappings = None
        self.diff_delta = None
        self.diff_rows = ()
        self.diff_decisions = {}
        self.diff_apply = None

    # --- IFC diff review (#981) ------------------------------------------

    def import_revised_ifc_source(
        self,
        source: bytes | str,
        *,
        file_name: str,
    ) -> tuple[IfcImportArtifact, IfcRevisionDelta, tuple[IfcDiffRow, ...]]:
        """Re-import a revised IFC and diff it against the bound base.

        The prior side is the mapping set the current subject was built
        from (``source_refs``-pinned import for a fresh subject, the
        merged set after an apply) — never "the newest artifact on disk",
        so a stale file re-uploaded later diffs against what the operator
        actually sees. Both artifacts + mappings are persisted, the sealed
        delta is stored, and the operator-facing rows are computed; no
        scene state changes until ``apply_ifc_diff`` commits.
        """
        prior_artifact = self.diff_base_artifact
        prior_mappings = self.diff_base_mappings
        if prior_artifact is None or prior_mappings is None:
            artifacts = self.ifc_repository.list_import_artifacts(
                self.document_id
            )
            if not artifacts:
                raise GeometryIntakeError(
                    'no bound IFC revision — import a base IFC first'
                )
            prior_artifact = artifacts[-1]
            # Reconciliation marks append re-sealed rows — reduce to the
            # latest record per entity before diffing (#981).
            prior_mappings = latest_entity_mappings(
                self.ifc_repository.list_entity_mappings(
                    self.document_id,
                    import_artifact_id=prior_artifact.artifact_id,
                )
            )
            self.diff_base_artifact = prior_artifact
            self.diff_base_mappings = tuple(prior_mappings)
        artifact, mappings = build_ifc_import(
            document_id=self.document_id,
            file_name=file_name,
            source=source,
            imported_at_utc=self._now(),
        )
        self.ifc_repository.save_import_artifact(artifact)
        self.ifc_repository.save_entity_mappings(mappings)
        delta = compute_ifc_revision_delta(
            document_id=self.document_id,
            prior_artifact=prior_artifact,
            prior_mappings=prior_mappings,
            new_artifact=artifact,
            new_mappings=mappings,
            evaluated_at_utc=self._now(),
        )
        self.ifc_repository.save_revision_delta(delta)
        rows = compute_ifc_diff_rows(
            prior_artifact=prior_artifact,
            prior_mappings=prior_mappings,
            new_artifact=artifact,
            new_mappings=mappings,
        )
        self.diff_new_artifact = artifact
        self.diff_new_mappings = tuple(mappings)
        self.diff_delta = delta
        self.diff_rows = rows
        # A delta may have been applied in an earlier session — restore
        # the recorded decisions so skipped rows re-open flagged.
        latest = self.ifc_repository.latest_diff_apply(
            self.document_id, delta.delta_id
        )
        self.diff_apply = latest
        self.diff_decisions = (
            {d.row_key: d for d in latest.decisions}
            if latest is not None
            else {}
        )
        return artifact, delta, rows

    def undecided_diff_row_keys(self) -> tuple[str, ...]:
        """Decision-required rows with no recorded call yet."""
        return tuple(
            row.row_key
            for row in self.diff_rows
            if row.decision_required
            and row.row_key not in self.diff_decisions
        )

    def submit_diff_decision(
        self, row_key: str, decision: IfcDiffDecision
    ) -> None:
        """Record the operator's accept/skip call on one diff row."""
        if self.diff_delta is None:
            raise GeometryIntakeError('no IFC diff staged')
        row = next(
            (r for r in self.diff_rows if r.row_key == row_key), None
        )
        if row is None:
            raise GeometryIntakeError(
                f'diff decision targets a row outside the diff: {row_key}'
            )
        if not row.decision_required:
            raise GeometryIntakeError(
                f'diff row {row_key} requires no decision'
            )
        if decision not in ('accepted', 'skipped'):
            raise GeometryIntakeError(
                f'unsupported diff decision: {decision}'
            )
        self.diff_decisions[row_key] = IfcDiffRowDecision(
            row_key=row_key,
            decision=decision,
            decided_by=self.operator_id,
            decided_at_utc=self._now(),
        )

    def set_all_diff_decisions(self, decision: IfcDiffDecision) -> None:
        """Bulk accept/skip across every decision-required row."""
        if self.diff_delta is None:
            raise GeometryIntakeError('no IFC diff staged')
        if decision not in ('accepted', 'skipped'):
            raise GeometryIntakeError(
                f'unsupported diff decision: {decision}'
            )
        now = self._now()
        for row in self.diff_rows:
            if row.decision_required:
                self.diff_decisions[row.row_key] = IfcDiffRowDecision(
                    row_key=row.row_key,
                    decision=decision,
                    decided_by=self.operator_id,
                    decided_at_utc=now,
                )

    def apply_ifc_diff(
        self,
    ) -> tuple[
        IfcDiffApply, GeometryIntakeReport, GeometryRepairProposal
    ]:
        """Commit the reviewed diff: merged subject + sealed apply record.

        All-or-nothing (the #996 bulk pattern): reconciliation marks on
        accepted rows, the sealed ``IfcDiffApply`` summary, and the merged
        subject's health-check records commit inside ONE ``BEGIN
        IMMEDIATE`` transaction — a failure anywhere leaves the store
        untouched and the prior subject remains the visible state.
        Undecided rows are recorded as ``pending`` and keep the prior
        side, never silently committed.
        """
        if self.diff_delta is None or not self.diff_rows:
            raise GeometryIntakeError(
                'no IFC diff staged — import a revised IFC first'
            )
        if (
            self.diff_base_artifact is None
            or self.diff_base_mappings is None
            or self.diff_new_artifact is None
            or self.diff_new_mappings is None
        ):
            raise GeometryIntakeError('diff base unavailable')
        decisions = {
            k: d.decision for k, d in self.diff_decisions.items()
        }
        prior_by_id = {m.mapping_id: m for m in self.diff_base_mappings}
        new_by_id = {m.mapping_id: m for m in self.diff_new_mappings}
        # Re-seal accepted mappings with their reconciliation state BEFORE
        # the merged subject pins them — parts reference the marked rows.
        replaced: dict[str, IfcEntityMapping] = {}
        for mapping_id, state in reconciliation_marks(
            rows=self.diff_rows,
            decisions=decisions,
            prior_mappings=self.diff_base_mappings,
            new_mappings=self.diff_new_mappings,
        ):
            source = new_by_id.get(mapping_id) or prior_by_id.get(
                mapping_id
            )
            if source is None:
                raise GeometryIntakeError(
                    f'reconciliation mark targets unknown {mapping_id}'
                )
            replaced[mapping_id] = mark_mapping_reconciliation(
                source, state
            )
        merged = tuple(
            replaced.get(m.mapping_id, m)
            for m in merge_ifc_diff_mappings(
                rows=self.diff_rows,
                decisions=decisions,
                prior_mappings=self.diff_base_mappings,
                new_mappings=self.diff_new_mappings,
            )
        )
        merged_subject = build_ifc_diff_subject(
            self.document_id,
            prior_artifact=self.diff_base_artifact,
            new_artifact=self.diff_new_artifact,
            delta=self.diff_delta,
            merged_mappings=merged,
        )
        # Fail-closed validation of the merged subject — the same
        # #866 health-check chain every subject goes through.
        report = diagnose_geometry_intake(
            merged_subject,
            evaluated_at_utc=self._now(),
            solver_context=self._solver_context(),
        )
        proposal = propose_geometry_repairs(
            report,
            merged_subject,
            proposed_by=self.operator_id,
            proposed_at_utc=self._now(),
            proposal_reason='ifc diff apply',
        )
        apply_record = build_ifc_diff_apply(
            document_id=self.document_id,
            delta=self.diff_delta,
            prior_artifact=self.diff_base_artifact,
            new_artifact=self.diff_new_artifact,
            prior_subject_sha256=(
                self.subject.subject_sha256
                if self.subject is not None
                else None
            ),
            merged_subject=merged_subject,
            decisions=tuple(self.diff_decisions.values()),
            rows=self.diff_rows,
            applied_by=self.operator_id,
            applied_at_utc=self._now(),
        )
        connection = connect_sqlite(self.ifc_repository.path)
        try:
            connection.execute('BEGIN IMMEDIATE')
            for marked in replaced.values():
                self.ifc_repository._save_entity_mapping_in_connection(
                    connection, marked
                )
            self.ifc_repository._save_diff_apply_in_connection(
                connection, apply_record
            )
            self.intake_repository.intake_reports.save_in_connection(
                connection, report
            )
            self.intake_repository.repair_proposals.save_in_connection(
                connection, proposal
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.subject = merged_subject
        self.artifact = None
        self.report = report
        self.proposal = proposal
        self.acceptance = None
        self.revision = None
        self.pending_decisions.clear()
        self.diff_base_artifact = self.diff_new_artifact
        self.diff_base_mappings = merged
        self.diff_apply = apply_record
        return apply_record, report, proposal

    def resume_diff_state(self) -> None:
        """Rebuild persisted diff-review state for re-open (#981).

        Recomputes the latest delta's rows from the stored artifacts —
        deterministic, so row keys match the recorded decisions — then
        restores the newest apply's accept/skip map. When an apply
        exists, the merged subject is rebuilt so ``self.subject``
        reflects the applied revision, and skipped rows stay visibly
        flagged.
        """
        deltas = self.ifc_repository.list_revision_deltas(
            self.document_id
        )
        if not deltas:
            return
        delta = deltas[-1]
        prior = self.ifc_repository.get_import_artifact(
            delta.prior_artifact_id
        )
        new = self.ifc_repository.get_import_artifact(
            delta.new_artifact_id
        )
        if prior is None or new is None:
            return
        prior_mappings = latest_entity_mappings(
            self.ifc_repository.list_entity_mappings(
                self.document_id,
                import_artifact_id=prior.artifact_id,
            )
        )
        new_mappings = latest_entity_mappings(
            self.ifc_repository.list_entity_mappings(
                self.document_id,
                import_artifact_id=new.artifact_id,
            )
        )
        self.diff_delta = delta
        self.diff_rows = compute_ifc_diff_rows(
            prior_artifact=prior,
            prior_mappings=prior_mappings,
            new_artifact=new,
            new_mappings=new_mappings,
        )
        self.diff_new_artifact = new
        self.diff_new_mappings = tuple(new_mappings)
        applies = self.ifc_repository.list_diff_applies(self.document_id)
        latest_for_delta = next(
            (
                a
                for a in reversed(applies)
                if a.delta_ref.ref_id == delta.delta_id
            ),
            None,
        )
        if latest_for_delta is not None:
            self.diff_apply = latest_for_delta
            self.diff_decisions = {
                d.row_key: d for d in latest_for_delta.decisions
            }
        else:
            self.diff_apply = None
            self.diff_decisions = {}
        # The live base is the merged set of the newest apply overall —
        # the subject the operator sees; before any apply it is the
        # delta's own prior side.
        last_apply = applies[-1] if applies else None
        if last_apply is None:
            self.diff_base_artifact = prior
            self.diff_base_mappings = tuple(prior_mappings)
            return
        base_delta = self.ifc_repository.get_revision_delta(
            last_apply.delta_ref.ref_id
        )
        if base_delta is None:
            self.diff_base_artifact = prior
            self.diff_base_mappings = tuple(prior_mappings)
            return
        base_prior = self.ifc_repository.get_import_artifact(
            base_delta.prior_artifact_id
        )
        base_new = self.ifc_repository.get_import_artifact(
            base_delta.new_artifact_id
        )
        if base_prior is None or base_new is None:
            self.diff_base_artifact = prior
            self.diff_base_mappings = tuple(prior_mappings)
            return
        base_prior_mappings = latest_entity_mappings(
            self.ifc_repository.list_entity_mappings(
                self.document_id,
                import_artifact_id=base_prior.artifact_id,
            )
        )
        base_new_mappings = latest_entity_mappings(
            self.ifc_repository.list_entity_mappings(
                self.document_id,
                import_artifact_id=base_new.artifact_id,
            )
        )
        base_rows = compute_ifc_diff_rows(
            prior_artifact=base_prior,
            prior_mappings=base_prior_mappings,
            new_artifact=base_new,
            new_mappings=base_new_mappings,
        )
        base_decisions = {
            d.row_key: d.decision for d in last_apply.decisions
        }
        merged = merge_ifc_diff_mappings(
            rows=base_rows,
            decisions=base_decisions,
            prior_mappings=base_prior_mappings,
            new_mappings=base_new_mappings,
        )
        self.subject = build_ifc_diff_subject(
            self.document_id,
            prior_artifact=base_prior,
            new_artifact=base_new,
            delta=base_delta,
            merged_mappings=merged,
        )
        self.artifact = None
        self.diff_base_artifact = base_new
        self.diff_base_mappings = merged

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
