"""Semantic authority-graph audit for the native CAD database.

Physical SQLite integrity, foreign-key checks and schema compatibility prove
that a database is *structurally* openable. They do not prove that every
persisted HTDT authority still re-derives exactly from its declared inputs —
a tampered ``payload_json`` row can carry a self-consistent shape while its
referenced SearchSpec, measurement evidence or upstream evaluation is stale,
missing or non-canonical.

``audit_native_authority_graph`` closes that gap by enumerating every
persisted authority row in dependency order and re-reading each one through
its domain repository's canonical read path — the same fail-closed replay
that production reads perform. No validation formula is duplicated here: a
probe either calls a repository getter (which re-resolves and re-derives the
authority or raises) or performs a byte-level evidence check.

Three verification tiers run per audit:

* **replay** — repository ``get_*``/``load`` calls re-resolve every external
  reference and re-derive canonical output, failing closed on stale or
  missing evidence;
* **structural** — ``payload_json`` bodies with no repository replay path
  (external-resolver domains such as solver results and multifidelity
  plans, plus link/legacy tables) must at least parse as their declared
  JSON payload;
* **evidence bytes** — managed-asset manifest rows must resolve to a
  content-addressed file whose SHA-256 and length match, and retained
  content blobs must match their digest key.

The audit target is always a clone: repository construction may create
missing tables, so callers stage/copy the database before auditing and the
live file is never opened here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore

if TYPE_CHECKING:
    from .cad_repository import SceneRepository

AuditFailureClass = Literal[
    'structural',
    'missing_evidence',
    'stale_authority',
    'noncanonical_derivation',
    'unclassified',
    'coverage_gap',
]

AuditCoverageMode = Literal[
    'replay_canonical',
    'evidence_bytes',
    'structural_only',
    'non_authority',
]


@dataclass(frozen=True)
class AuthorityAuditDiagnostic:
    """One failing persisted authority, with the dependency that rejected it."""

    authority: str
    record_ref: str
    failure_class: AuditFailureClass
    dependency: str
    message: str


@dataclass(frozen=True)
class AuthorityAuditReport:
    """Result of one semantic authority-graph audit."""

    database_path: Path
    checked: tuple[tuple[str, int], ...]
    diagnostics: tuple[AuthorityAuditDiagnostic, ...]
    #: Tables that carried neither a replay probe, an evidence-bytes check,
    #: nor an explicit coverage-policy entry. Always empty on a passing
    #: report — an unclassified table means the audit never saw its rows
    #: and cannot claim coverage for them.
    unclassified_tables: tuple[str, ...] = ()
    coverage: tuple[tuple[str, AuditCoverageMode, int], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.diagnostics and not self.unclassified_tables

    @property
    def coverage_counts(self) -> dict[str, int]:
        """Tables covered per coverage class (replayed/evidence_bytes/
        structural_only/operational_metadata) plus unclassified count."""
        counts = {
            'replayed_records': 0,
            'structural_only_tables': 0,
            'operational_metadata_tables': 0,
            'unclassified_tables': len(self.unclassified_tables),
        }
        for name, count in self.checked:
            if name.startswith('structural:'):
                counts['structural_only_tables'] += 1
            elif name.startswith('metadata:'):
                counts['operational_metadata_tables'] += 1
            else:
                counts['replayed_records'] += count
        return counts

    def coverage_summary(self) -> dict[AuditCoverageMode, int]:
        """Row counts per audit coverage mode (0 when a mode never ran)."""
        totals: dict[AuditCoverageMode, int] = {
            'replay_canonical': 0,
            'evidence_bytes': 0,
            'structural_only': 0,
            'non_authority': 0,
        }
        for _label, mode, count in self.coverage:
            totals[mode] += count
        return totals

    def summary(self) -> str:
        coverage = self.coverage_summary()
        coverage_note = (
            ' [replay={replay_canonical} evidence_bytes={evidence_bytes} '
            'structural_only={structural_only} '
            'non_authority={non_authority}]'
        ).format(**coverage)
        if self.ok:
            total = sum(count for _, count in self.checked)
            counts = self.coverage_counts
            return (
                f'authority graph audit passed ({total} records checked; '
                f"{counts['replayed_records']} replayed, "
                f"{counts['structural_only_tables']} structural-only tables, "
                f"{counts['operational_metadata_tables']} operational "
                'metadata tables, 0 unclassified)'
                f'{coverage_note}'
            )
        lines = [
            f'authority graph audit failed ({len(self.diagnostics)} diagnostics):'
        ]
        for diagnostic in self.diagnostics[:20]:
            lines.append(
                f'  [{diagnostic.authority}:{diagnostic.record_ref}] '
                f'{diagnostic.failure_class} — {diagnostic.message}'
            )
        if len(self.diagnostics) > 20:
            lines.append(f'  ... {len(self.diagnostics) - 20} more')
        for table in self.unclassified_tables[:10]:
            lines.append(f'  [coverage:{table}] unclassified table')
        if len(self.unclassified_tables) > 10:
            lines.append(
                f'  ... {len(self.unclassified_tables) - 10} more unclassified'
            )
        return '\n'.join(lines)


class AuthorityAuditError(ValueError):
    """Raised when the persisted authority graph fails semantic replay."""

    def __init__(self, report: AuthorityAuditReport) -> None:
        super().__init__(report.summary())
        self.report = report


class _RepositoryChain:
    """Canonical repository graph over one audit database.

    Repositories are constructed lazily in dependency order so the audit
    only pays for domains that actually persist rows.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._repos: dict[str, Any] = {}
        # Read-only replay memos shared across every probe row: persisted
        # authority cannot change mid-audit, so per-parent listings, the
        # objective repository's candidate-set scans, and Room Simulator
        # batch-authority replays are computed once.
        self._lists: dict[tuple[str, Any], Any] = {}
        self.objective_scans: dict[str, Any] = {}
        self.roomsim_batches: dict[str, Any] = {}

    def list_once(self, label: str, key: Any, load: Callable[[], Any]) -> Any:
        """Return the canonical per-parent listing, loading it once.

        Probes re-list the same collection for every child row; a listing
        that raises is not cached, so each row still reports the failure a
        fresh listing would produce.
        """
        cache_key = (label, key)
        if cache_key not in self._lists:
            self._lists[cache_key] = load()
        return self._lists[cache_key]

    def repo(self, name: str) -> Any:
        if name in self._repos:
            return self._repos[name]
        repo = self._build(name)
        self._repos[name] = repo
        return repo

    def _build(self, name: str) -> Any:  # noqa: C901
        if name == 'scene':
            from .cad_repository import SceneRepository

            return SceneRepository(self.db_path)
        scene = self.repo('scene')
        if name == 'search':
            from .cad_search_repository import CadSearchRepository

            return CadSearchRepository(scene)
        if name == 'constraints':
            from .cad_constraint_repository import CadConstraintRepository

            return CadConstraintRepository(self.db_path)
        if name == 'measurement':
            from .cad_measurement_repository import CadMeasurementRepository

            return CadMeasurementRepository(scene)
        if name == 'quality':
            from .cad_measurement_quality_repository import (
                CadMeasurementQualityRepository,
            )

            return CadMeasurementQualityRepository(self.repo('measurement'))
        if name == 'roomsim':
            from .cad_roomsim_repository import CadRoomSimRepository

            return CadRoomSimRepository(scene, self.repo('search'))
        if name == 'objective':
            from .cad_objective_repository import CadObjectiveRepository

            return CadObjectiveRepository(
                scene,
                self.repo('search'),
                measurement_repository=self.repo('measurement'),
                roomsim_repository=self.repo('roomsim'),
            )
        if name == 'validation':
            from .cad_model_validation_repository import (
                CadModelValidationRepository,
            )

            return CadModelValidationRepository(
                self.repo('search'),
                self.repo('roomsim'),
                self.repo('measurement'),
                self.repo('objective'),
            )
        if name == 'adaptive':
            from .cad_adaptive_repository import CadAdaptivePlanRepository

            return CadAdaptivePlanRepository(
                self.repo('search'),
                self.repo('validation'),
                self.repo('objective'),
            )
        if name == 'extended':
            from .cad_extended_search_repository import (
                CadExtendedSearchRepository,
            )

            return CadExtendedSearchRepository(
                self.repo('search'), self.repo('validation')
            )
        if name == 'adaptive_extended':
            from .cad_adaptive_extended_repository import (
                CadAdaptiveExtendedRepository,
            )

            return CadAdaptiveExtendedRepository(
                self.repo('extended'),
                self.repo('validation'),
                self.repo('objective'),
            )
        if name == 'campaign':
            from .cad_validation_campaign_repository import (
                CadValidationCampaignRepository,
            )

            return CadValidationCampaignRepository(
                self.repo('search'), self.repo('measurement')
            )
        if name == 'variant':
            from .cad_system_variant_repository import (
                CadSystemVariantRepository,
            )

            return CadSystemVariantRepository(scene)
        if name == 'equipment':
            from .cad_equipment_repository import CadEquipmentRepository

            return CadEquipmentRepository(scene, self.repo('variant'))
        if name == 'directivity':
            from .cad_directivity_repository import CadDirectivityRepository

            return CadDirectivityRepository(scene, self.repo('equipment'))
        if name == 'coverage':
            from .cad_coverage_repository import CadCoverageRepository

            return CadCoverageRepository(
                scene,
                self.repo('variant'),
                self.repo('equipment'),
                self.repo('directivity'),
            )
        if name == 'direct_level':
            from .cad_direct_level_repository import CadDirectLevelRepository

            return CadDirectLevelRepository(
                scene, self.repo('variant'), self.repo('equipment')
            )
        if name == 'headroom':
            from .cad_amplifier_headroom_repository import (
                CadAmplifierHeadroomRepository,
            )

            return CadAmplifierHeadroomRepository(
                scene, self.repo('variant'), self.repo('equipment')
            )
        if name == 'standards':
            from .cad_standards_repository import CadStandardsRepository

            return CadStandardsRepository(scene, self.repo('variant'))
        if name == 'topology':
            from .cad_topology_comparison_repository import (
                CadTopologyComparisonRepository,
            )

            return CadTopologyComparisonRepository(
                scene_repository=scene,
                system_variant_repository=self.repo('variant'),
                standards_repository=self.repo('standards'),
                coverage_repository=self.repo('coverage'),
                direct_level_repository=self.repo('direct_level'),
                amplifier_headroom_repository=self.repo('headroom'),
            )
        if name == 'robustness':
            from .cad_robustness_repository import CadRobustnessRepository

            return CadRobustnessRepository(
                scene_repository=scene,
                search_repository=self.repo('search'),
                objective_repository=self.repo('objective'),
                extended_search_repository=self.repo('extended'),
            )
        if name == 'robustness_validation':
            from .cad_robustness_validation_repository import (
                CadRobustnessValidationRepository,
            )

            return CadRobustnessValidationRepository(
                robustness_repository=self.repo('robustness'),
                model_validation_repository=self.repo('validation'),
                campaign_repository=self.repo('campaign'),
                measurement_repository=self.repo('measurement'),
                quality_repository=self.repo('quality'),
                roomsim_repository=self.repo('roomsim'),
            )
        if name == 'calibration':
            from .cad_calibration_repository import CadCalibrationRepository

            return CadCalibrationRepository(
                scene_repository=scene,
                system_variant_repository=self.repo('variant'),
                measurement_repository=self.repo('measurement'),
                quality_repository=self.repo('quality'),
            )
        if name == 'model_calibration':
            from .cad_model_calibration_repository import (
                CadModelCalibrationRepository,
            )

            return CadModelCalibrationRepository(scene)
        if name == 'joint':
            from .cad_joint_optimization_repository import (
                CadJointOptimizationRepository,
            )

            return CadJointOptimizationRepository(
                scene_repository=scene,
                system_variant_repository=self.repo('variant'),
                calibration_repository=self.repo('calibration'),
                search_repository=self.repo('search'),
                extended_search_repository=self.repo('extended'),
                robustness_repository=self.repo('robustness'),
            )
        if name == 'treatment':
            from .cad_acoustic_treatment_repository import (
                CadAcousticTreatmentRepository,
            )

            return CadAcousticTreatmentRepository(scene, self.repo('variant'))
        if name == 'prediction':
            from .cad_prediction_repository import CadPredictionRepository

            return CadPredictionRepository(scene)
        if name == 'lifecycle':
            from .cad_system_variant_lifecycle import (
                CadSystemVariantLifecycleRepository,
            )

            return CadSystemVariantLifecycleRepository(
                scene_repository=scene,
                variant_repository=self.repo('variant'),
            )
        if name == 'measured':
            from .cad_system_variant_measured_lifecycle import (
                CadSystemVariantMeasuredLifecycleRepository,
            )

            return CadSystemVariantMeasuredLifecycleRepository(
                scene_repository=scene,
                lifecycle_repository=self.repo('lifecycle'),
                measurement_repository=self.repo('measurement'),
                quality_repository=self.repo('quality'),
            )
        if name == 'measurement_campaign':
            from .cad_system_variant_measurement_campaign import (
                CadSystemVariantMeasurementCampaignRepository,
            )

            return CadSystemVariantMeasurementCampaignRepository(
                scene_repository=scene,
                variant_repository=self.repo('variant'),
                lifecycle_repository=self.repo('lifecycle'),
                measurement_repository=self.repo('measurement'),
                quality_repository=self.repo('quality'),
                measured_lifecycle_repository=self.repo('measured'),
            )
        if name == 'r110':
            from .cad_r110_source_repository import CadR110SourceRepository

            return CadR110SourceRepository(
                scene,
                variant_repository=self.repo('variant'),
                equipment_repository=self.repo('equipment'),
                directivity_repository=self.repo('directivity'),
            )
        if name == 'r120':
            from .r120_geometry_compiler_repository import (
                R120GeometryCompilerRepository,
            )

            return R120GeometryCompilerRepository(scene)
        if name == 'overlay':
            from .treatment_boundary_overlay_repository import (
                TreatmentBoundaryOverlayRepository,
            )

            return TreatmentBoundaryOverlayRepository(
                scene_repository=scene,
                treatment_repository=self.repo('treatment'),
                r120_repository=self.repo('r120'),
            )
        if name == 'wave':
            from .cad_wave_excitation import CadWaveExcitationRepository

            return CadWaveExcitationRepository(
                scene,
                equipment_repository=self.repo('equipment'),
                r110_repository=self.repo('r110'),
            )
        if name == 'snapshot':
            from .cad_acoustic_snapshot_repository import (
                CadAcousticSnapshotRepository,
            )

            return CadAcousticSnapshotRepository(
                scene,
                variant_repository=self.repo('variant'),
                r110_repository=self.repo('r110'),
                r120_repository=self.repo('r120'),
                treatment_boundary_repository=self.repo('overlay'),
                wave_excitation_repository=self.repo('wave'),
            )
        if name == 'video':
            from .cad_video_geometry_repository import (
                CadVideoGeometryRepository,
            )

            return CadVideoGeometryRepository(scene, self.repo('variant'))
        if name == 'applicability':
            from .cad_applicability import CadApplicabilityAttestationRepository

            return CadApplicabilityAttestationRepository(self.db_path)
        if name == 'capture':
            from .capture_ingestion_transaction import (
                CaptureIngestionRepository,
            )

            return CaptureIngestionRepository(scene)
        if name == 'comparison':
            from .cad_design_comparison_repository import (
                CadDesignComparisonRepository,
            )

            return CadDesignComparisonRepository(scene)
        if name == 'presets':
            from .cad_operating_preset_repository import (
                CadOperatingPresetRepository,
            )

            return CadOperatingPresetRepository(scene)
        if name == 'health':
            from .cad_system_health_repository import (
                CadSystemHealthRepository,
            )

            return CadSystemHealthRepository(scene)
        if name == 'checkpoints':
            from .cad_design_checkpoint_repository import (
                CadDesignCheckpointRepository,
            )

            return CadDesignCheckpointRepository(scene)
        if name == 'project_library':
            from .project_lifecycle import ProjectLibrary

            return ProjectLibrary(self.db_path)
        if name == 'design_comparisons':
            from .cad_design_comparison_repository import (
                CadDesignComparisonRepository,
            )

            return CadDesignComparisonRepository(scene)
        if name == 'decisions':
            from .cad_design_decision_repository import (
                CadDesignDecisionRepository,
            )

            return CadDesignDecisionRepository(scene)
        if name == 'briefs':
            from .cad_design_brief_repository import (
                CadDesignBriefRepository,
            )

            return CadDesignBriefRepository(scene)
        if name == 'studies':
            from .cad_analysis_study_repository import (
                CadAnalysisStudyRepository,
            )

            return CadAnalysisStudyRepository(scene)
        if name == 'assumptions':
            from .cad_assumption_decision_repository import (
                CadAssumptionDecisionRepository,
            )

            return CadAssumptionDecisionRepository(scene)
        if name == 'installed':
            from .cad_equipment_instance_repository import (
                CadInstalledEquipmentRepository,
            )

            return CadInstalledEquipmentRepository(
                scene, self.repo('equipment')
            )
        if name == 'upgrades':
            from .cad_library_upgrade_repository import (
                CadLibraryUpgradeRepository,
            )

            return CadLibraryUpgradeRepository(scene, self.repo('equipment'))
        if name == 'equipment_bindings':
            from .cad_equipment_binding_repository import (
                CadEquipmentBindingRepository,
            )

            return CadEquipmentBindingRepository(
                scene, self.repo('equipment')
            )
        if name == 'runner':
            from .cad_measurement_runner_repository import (
                CadMeasurementRunnerRepository,
            )

            return CadMeasurementRunnerRepository(scene)
        if name == 'dependencies':
            from .external_dependency_repository import (
                ExternalDependencyRepository,
            )

            return ExternalDependencyRepository(scene)
        if name == 'projects':
            from .project_lifecycle import ProjectLibrary

            return ProjectLibrary(self.db_path)
        if name == 'field_explorer':
            from .cad_field_explorer_repository import (
                CadFieldExplorerRepository,
            )

            return CadFieldExplorerRepository(scene)
        raise KeyError(name)


@dataclass(frozen=True)
class _ReplayProbe:
    """Enumerate ``columns`` from ``table`` and re-read each row canonically."""

    authority: str
    table: str
    columns: tuple[str, ...]
    verify: Callable[[_RepositoryChain, tuple[Any, ...]], Any]


def _get(repo: str, method: str) -> Callable[[_RepositoryChain, tuple[Any, ...]], Any]:
    def _verify(chain: _RepositoryChain, key: tuple[Any, ...]) -> Any:
        result = getattr(chain.repo(repo), method)(*key)
        if result is None:
            raise ValueError(f'{repo} authority {key} no longer resolves')
        return result

    return _verify


def _verify_measurement_plan(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Replay the plan's single-head chain via its owning SearchSpec."""

    plan_id, search_spec_id = key
    plans = chain.list_once(
        'measurement_plans',
        search_spec_id,
        lambda: chain.repo('measurement').list_measurement_plans(search_spec_id),
    )
    if plan_id not in {plan.plan_id for plan in plans}:
        raise ValueError(
            f'measurement plan {plan_id} no longer resolves in its '
            f'canonical chain for SearchSpec {search_spec_id}'
        )
    return plans


def _verify_measurement_lineage(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Replay the document's retake lineage chains via the quality repo."""

    lineage_id, document_id = key
    lineage = chain.list_once(
        'measurement_lineage',
        document_id,
        lambda: chain.repo('quality').list_lineage(document_id),
    )
    if lineage_id not in {record.lineage_id for record in lineage}:
        raise ValueError(
            f'measurement lineage {lineage_id} no longer resolves in its '
            f'canonical chain for document {document_id}'
        )
    return lineage


def _verify_robustness_sample(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Replay the spec's exact perturbation sample set."""

    sample_id, robustness_spec_id = key
    samples = chain.list_once(
        'robustness_samples',
        robustness_spec_id,
        lambda: chain.repo('robustness').list_samples(robustness_spec_id),
    )
    if sample_id not in {sample.sample_id for sample in samples}:
        raise ValueError(
            f'perturbation sample {sample_id} no longer resolves for '
            f'RobustnessSpec {robustness_spec_id}'
        )
    return samples


def _verify_robustness_evaluation(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Replay the spec's exact robustness evaluation set."""

    evaluation_id, robustness_spec_id = key
    evaluations = chain.list_once(
        'robustness_evaluations',
        robustness_spec_id,
        lambda: chain.repo('robustness').list_evaluations(robustness_spec_id),
    )
    if evaluation_id not in {
        evaluation.evaluation_id for evaluation in evaluations
    }:
        raise ValueError(
            f'robustness evaluation {evaluation_id} no longer resolves for '
            f'RobustnessSpec {robustness_spec_id}'
        )
    return evaluations


def _verify_roomsim_batch(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Re-attest one batch spec over the run's shared batch replay memo."""

    result = chain.repo('roomsim').get_batch_spec(
        key[0], batches=chain.roomsim_batches
    )
    if result is None:
        raise ValueError(f'roomsim batch authority {key} no longer resolves')
    return result


def _verify_roomsim_attempt(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Replay the batch's canonical attempt list once per batch_run_id.

    ``list_attempts`` validates every row's payload columns and each
    completed attempt against the shared batch authority — strictly more
    coverage than ``get_attempt``'s per-row read — so membership in the
    canonical list satisfies the probe, while one batch-authority replay
    per batch replaces the per-attempt refetch.
    """

    attempt_id, batch_run_id = key
    attempts = chain.list_once(
        'roomsim_attempts',
        batch_run_id,
        lambda: chain.repo('roomsim').list_attempts(
            batch_run_id, batches=chain.roomsim_batches
        ),
    )
    if attempt_id not in {item.attempt_id for item in attempts}:
        raise ValueError(
            f'Room Simulator attempt {attempt_id} no longer resolves in its '
            f'canonical list for batch {batch_run_id}'
        )
    return attempts


def _verify_objective_evaluation(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Re-attest one O30 row over the run's shared candidate-set scans.

    ``get_evaluation`` regenerates a SearchSpec's canonical candidate set
    per call; sharing ``chain.objective_scans`` across rows pays that replay
    once per SearchSpec while preserving per-row failure semantics. The
    Room Simulator batch memo likewise deduplicates the batch authority
    each predicted input reference resolves against.
    """

    result = chain.repo('objective').get_evaluation(
        key[0], scans=chain.objective_scans, batches=chain.roomsim_batches
    )
    if result is None:
        raise ValueError(f'objective authority {key} no longer resolves')
    return result


def _require(record: Any, description: str) -> Any:
    """Fail closed when a canonical getter returns ``None``."""

    if record is None:
        raise ValueError(f'{description} no longer resolves')
    return record


def _verify_scene_head(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Every explicit document head must resolve to its SceneRevision."""

    (document_id,) = key
    return _require(
        chain.repo('scene').current_head(document_id),
        f'current head for document {document_id}',
    )


def _verify_constraint_head(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """The constraint-set singleton must resolve to a sealed head revision."""

    (document_id,) = key
    scene = chain.repo('scene')
    _require(
        scene.authoring_constraint_head(document_id),
        f'authoring constraint head for document {document_id}',
    )
    return scene.authoring_constraints(document_id)


def _verify_constraint_revision(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Constraint revisions form a single-successor chain per document."""

    constraint_revision_id, document_id = key
    lineage = chain.repo('scene').list_authoring_constraint_revisions(
        document_id
    )
    record = next(
        (
            r
            for r in lineage
            if r.constraint_revision_id == constraint_revision_id
        ),
        None,
    )
    _require(
        record,
        f'authoring constraint revision {constraint_revision_id} in '
        f'document {document_id}',
    )
    lineage_ids = {r.constraint_revision_id for r in lineage}
    if record.supersedes_id is not None and (
        record.supersedes_id not in lineage_ids
    ):
        raise ValueError(
            f'constraint revision {constraint_revision_id} supersedes '
            f'{record.supersedes_id}, which is absent from the lineage'
        )
    if sum(1 for r in lineage if r.supersedes_id == constraint_revision_id) > 1:
        raise ValueError(
            f'branched constraint lineage: multiple revisions supersede '
            f'{constraint_revision_id}'
        )
    return record


def _verify_measurement_disposition(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    disposition_id, measurement_id = key
    quality = chain.repo('quality')
    disposition = _require(
        quality.get_disposition(disposition_id),
        f'measurement disposition {disposition_id}',
    )
    if disposition.measurement_id != measurement_id:
        raise ValueError(
            f'disposition {disposition_id} binds measurement '
            f'{disposition.measurement_id}, not recorded {measurement_id}'
        )
    _require(
        chain.repo('measurement').get_measurement(measurement_id),
        f'measurement {measurement_id} referenced by disposition '
        f'{disposition_id}',
    )
    if disposition.correction_id is not None:
        _require(
            quality.get_correction(disposition.correction_id),
            f'correction {disposition.correction_id} referenced by '
            f'disposition {disposition_id}',
        )
    return disposition


def _verify_measurement_correction(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    correction_id, measurement_id = key
    correction = _require(
        chain.repo('quality').get_correction(correction_id),
        f'measurement correction {correction_id}',
    )
    if correction.measurement_id != measurement_id:
        raise ValueError(
            f'correction {correction_id} binds measurement '
            f'{correction.measurement_id}, not recorded {measurement_id}'
        )
    _require(
        chain.repo('measurement').get_measurement(measurement_id),
        f'measurement {measurement_id} referenced by correction '
        f'{correction_id}',
    )
    return correction


def _verify_applied_preset(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    applied_id, preset_id = key
    applied = chain.repo('presets').verify_persisted_applied_state(applied_id)
    if applied.preset_id != preset_id:
        raise ValueError(
            f'applied preset state {applied_id} binds preset '
            f'{applied.preset_id}, not recorded {preset_id}'
        )
    return applied


def _verify_preset_binding(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    binding_id, preset_id = key
    binding = chain.repo('presets').verify_persisted_measurement_binding(
        binding_id
    )
    if binding.preset_id != preset_id:
        raise ValueError(
            f'preset binding {binding_id} binds preset {binding.preset_id}, '
            f'not recorded {preset_id}'
        )
    return binding


def _verify_checkpoint_restore(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    restore_id, checkpoint_id = key
    restore = chain.repo('checkpoints').verify_persisted_restore(restore_id)
    if restore.checkpoint_id != checkpoint_id:
        raise ValueError(
            f'checkpoint restore {restore_id} binds checkpoint '
            f'{restore.checkpoint_id}, not recorded {checkpoint_id}'
        )
    return restore


def _verify_health_plan(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    plan_id, baseline_id = key
    plan = chain.repo('health').verify_persisted_plan(plan_id)
    if plan.baseline_id != baseline_id:
        raise ValueError(
            f'health check plan {plan_id} binds baseline '
            f'{plan.baseline_id}, not recorded {baseline_id}'
        )
    return plan


def _verify_health_run(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    run_id, plan_id = key
    run = chain.repo('health').verify_persisted_run(run_id)
    if run.plan_id != plan_id:
        raise ValueError(
            f'health check run {run_id} binds plan {run.plan_id}, '
            f'not recorded {plan_id}'
        )
    return run


def _verify_supersedes_chain(
    record: Any,
    lineage: Any,
    id_attr: str,
    supersedes_attr: str,
    description: str,
) -> Any:
    """Single-successor supersedes chain within one canonical lineage."""

    lineage_ids = {getattr(item, id_attr) for item in lineage}
    record_id = getattr(record, id_attr)
    supersedes = getattr(record, supersedes_attr, None)
    if supersedes is not None and supersedes not in lineage_ids:
        raise ValueError(
            f'{description} {record_id} supersedes {supersedes}, which is '
            f'absent from its canonical lineage'
        )
    if (
        sum(
            1
            for item in lineage
            if getattr(item, supersedes_attr, None) == record_id
        )
        > 1
    ):
        raise ValueError(
            f'branched {description} lineage: multiple records supersede '
            f'{record_id}'
        )
    return record


def _verify_comparison_set(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    set_id, document_id = key
    comparisons = chain.repo('design_comparisons')
    record = comparisons.verify_persisted_set(set_id)
    if record.document_id != document_id:
        raise ValueError(
            f'comparison set {set_id} binds document '
            f'{record.document_id}, not recorded {document_id}'
        )
    return _verify_supersedes_chain(
        record,
        comparisons.list_sets(document_id),
        'set_id',
        'supersedes_set_id',
        'comparison set',
    )


def _verify_design_decision(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    decision_id, document_id = key
    decisions = chain.repo('decisions').list_decisions(document_id)
    record = _require(
        next((d for d in decisions if d.decision_id == decision_id), None),
        f'design decision {decision_id} in document {document_id}',
    )
    return _verify_supersedes_chain(
        record,
        decisions,
        'decision_id',
        'supersedes_decision_id',
        'design decision',
    )


def _verify_design_brief(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    brief_id, document_id = key
    briefs = chain.repo('briefs').list_briefs(document_id)
    record = _require(
        next((b for b in briefs if b.brief_id == brief_id), None),
        f'design brief {brief_id} in document {document_id}',
    )
    return _verify_supersedes_chain(
        record, briefs, 'brief_id', 'supersedes_brief_id', 'design brief'
    )


def _verify_analysis_study(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    study_id, document_id = key
    studies = chain.repo('studies').list_studies(document_id)
    record = _require(
        next((s for s in studies if s.study_id == study_id), None),
        f'analysis study {study_id} in document {document_id}',
    )
    return _verify_supersedes_chain(
        record, studies, 'study_id', 'supersedes_study_id', 'analysis study'
    )


def _verify_assumption_decision(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    decision_id, document_id = key
    decisions = chain.repo('assumptions').list_decisions(document_id)
    record = _require(
        next((d for d in decisions if d.decision_id == decision_id), None),
        f'assumption decision {decision_id} in document {document_id}',
    )
    return _verify_supersedes_chain(
        record,
        decisions,
        'decision_id',
        'supersedes_decision_id',
        'assumption decision',
    )


def _verify_external_dependency(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    dependency_id, document_id = key
    dependency = _require(
        chain.repo('dependencies').get_dependency(dependency_id),
        f'external dependency {dependency_id}',
    )
    if dependency.document_id != document_id:
        raise ValueError(
            f'external dependency {dependency_id} binds document '
            f'{dependency.document_id}, not recorded {document_id}'
        )
    return dependency


def _verify_dependency_event(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    event_id, dependency_id = key
    dependencies = chain.repo('dependencies')
    _require(
        dependencies.get_dependency(dependency_id),
        f'external dependency {dependency_id}',
    )
    events = dependencies.list_resolutions(dependency_id)
    if event_id not in {event.event_id for event in events}:
        raise ValueError(
            f'resolution event {event_id} no longer resolves for '
            f'dependency {dependency_id}'
        )
    return events


def _verify_upgrade_adoption(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    adoption_id, document_id = key
    upgrades = chain.repo('upgrades')
    adoptions = upgrades.adoptions_for_document(document_id)
    record = _require(
        next((a for a in adoptions if a.adoption_id == adoption_id), None),
        f'upgrade adoption {adoption_id} in document {document_id}',
    )
    _require(
        upgrades.get_upgrade_by_hash(record.upgrade_sha256),
        f'equipment upgrade {record.upgrade_sha256} adopted by '
        f'{adoption_id}',
    )
    return record


def _verify_installed_binding(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    binding_id, instance_id = key
    installed = chain.repo('installed')
    _require(
        installed.get_instance(instance_id),
        f'installed equipment instance {instance_id}',
    )
    bindings = installed.list_bindings(instance_id)
    if binding_id not in {binding.binding_id for binding in bindings}:
        raise ValueError(
            f'definition binding {binding_id} no longer resolves for '
            f'instance {instance_id}'
        )
    return bindings


def _verify_installed_observation(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    observation_id, instance_id = key
    installed = chain.repo('installed')
    _require(
        installed.get_instance(instance_id),
        f'installed equipment instance {instance_id}',
    )
    observations = installed.list_observations(instance_id)
    if observation_id not in {
        observation.observation_id for observation in observations
    }:
        raise ValueError(
            f'device observation {observation_id} no longer resolves for '
            f'instance {instance_id}'
        )
    return observations


def _verify_installed_replacement(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    replacement_id, document_id = key
    installed = chain.repo('installed')
    replacements = installed.list_replacements(document_id)
    record = _require(
        next(
            (r for r in replacements if r.replacement_id == replacement_id),
            None,
        ),
        f'installed equipment replacement {replacement_id} in document '
        f'{document_id}',
    )
    _require(
        installed.get_instance(record.removed_instance_id),
        f'removed instance {record.removed_instance_id}',
    )
    _require(
        installed.get_instance(record.installed_instance_id),
        f'installed instance {record.installed_instance_id}',
    )
    if (
        sum(
            1
            for r in replacements
            if r.removed_instance_id == record.removed_instance_id
        )
        > 1
    ):
        raise ValueError(
            f'branched installed-equipment lineage: instance '
            f'{record.removed_instance_id} has multiple replacements'
        )
    if (
        sum(
            1
            for r in replacements
            if r.installed_instance_id == record.installed_instance_id
        )
        > 1
    ):
        raise ValueError(
            f'installed instance {record.installed_instance_id} is the '
            f'successor of multiple removals'
        )
    return record


def _verify_runner_run(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    run_id, plan_id = key
    runner = chain.repo('runner')
    run = _require(runner.get_run(run_id), f'measurement runner run {run_id}')
    if run.plan_id != plan_id:
        raise ValueError(
            f'measurement runner run {run_id} binds plan {run.plan_id}, '
            f'not recorded {plan_id}'
        )
    _require(
        runner.get_plan(plan_id), f'measurement runner plan {plan_id}'
    )
    return run


def _verify_runner_event(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    event_id, run_id = key
    runner = chain.repo('runner')
    _require(runner.get_run(run_id), f'measurement runner run {run_id}')
    events = runner.list_events(run_id)
    if event_id not in {event.event_id for event in events}:
        raise ValueError(
            f'runner event {event_id} no longer resolves for run {run_id}'
        )
    return events


def _verify_calibration_result(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    result_id, spec_id = key
    repository = chain.repo('model_calibration')
    result = _require(
        repository.get_result(result_id),
        f'model calibration result {result_id}',
    )
    if result.spec_id != spec_id:
        raise ValueError(
            f'model calibration result {result_id} binds spec '
            f'{result.spec_id}, not recorded {spec_id}'
        )
    _require(
        repository.get_spec(spec_id),
        f'model calibration spec {spec_id}',
    )
    return result


def _verify_calibration_model(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    materialized_model_id, calibration_result_id = key
    repository = chain.repo('model_calibration')
    model = _require(
        repository.get_model(materialized_model_id),
        f'calibrated model {materialized_model_id}',
    )
    if model.calibration_result_id != calibration_result_id:
        raise ValueError(
            f'calibrated model {materialized_model_id} binds result '
            f'{model.calibration_result_id}, not recorded '
            f'{calibration_result_id}'
        )
    _require(
        repository.get_result(calibration_result_id),
        f'model calibration result {calibration_result_id}',
    )
    return model


def _verify_calibration_freeze(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    freeze_id, calibration_result_id = key
    repository = chain.repo('model_calibration')
    freeze = _require(
        repository.get_freeze(freeze_id),
        f'calibration freeze {freeze_id}',
    )
    if freeze.calibration_result_id != calibration_result_id:
        raise ValueError(
            f'calibration freeze {freeze_id} binds result '
            f'{freeze.calibration_result_id}, not recorded '
            f'{calibration_result_id}'
        )
    _require(
        repository.get_result(calibration_result_id),
        f'model calibration result {calibration_result_id}',
    )
    return freeze


def _verify_calibration_holdout(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    record_id, freeze_id = key
    repository = chain.repo('model_calibration')
    record = _require(
        repository.get_holdout_record(record_id),
        f'holdout discipline record {record_id}',
    )
    if record.freeze_id != freeze_id:
        raise ValueError(
            f'holdout discipline record {record_id} binds freeze '
            f'{record.freeze_id}, not recorded {freeze_id}'
        )
    _require(
        repository.get_freeze(freeze_id),
        f'calibration freeze {freeze_id}',
    )
    return record


def _verify_calibration_evidence_event(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    (
        seq,
        campaign_id,
        campaign_sha256,
        consumption_kind,
        freeze_id,
        record_id,
    ) = key
    repository = chain.repo('model_calibration')
    if not campaign_id or not isinstance(campaign_sha256, str) or not all(
        c in '0123456789abcdef' for c in campaign_sha256
    ) or len(campaign_sha256) != 64:
        raise ValueError(
            f'calibration evidence event {seq} carries a malformed '
            'campaign authority ref'
        )
    if not consumption_kind:
        raise ValueError(
            f'calibration evidence event {seq} lacks a consumption kind'
        )
    if freeze_id is not None:
        _require(
            repository.get_freeze(freeze_id),
            f'calibration freeze {freeze_id} claimed by evidence event {seq}',
        )
    if record_id is not None:
        record = _require(
            repository.get_holdout_record(record_id),
            f'holdout discipline record {record_id} claimed by evidence '
            f'event {seq}',
        )
        if freeze_id is not None and record.freeze_id != freeze_id:
            raise ValueError(
                f'calibration evidence event {seq} binds freeze {freeze_id} '
                f'but holdout record {record_id} binds {record.freeze_id}'
            )
    return key


def _verify_project_tombstone(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    tombstone_id, project_id = key
    record = chain.repo('project_library').verify_persisted_tombstone(
        tombstone_id
    )
    if str(record['project_id']) != project_id:
        raise ValueError(
            f'project tombstone {tombstone_id} binds project '
            f"{record['project_id']}, not recorded {project_id}"
        )
    return record


def _verify_model_calibration_spec(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    spec_id, semantic_sha256 = key
    spec = _require(
        chain.repo('model_calibration').get_spec(spec_id),
        f'model calibration spec {spec_id}',
    )
    if spec.semantic_sha256 != semantic_sha256:
        raise ValueError(
            f'model calibration spec {spec_id} re-derives semantic hash '
            f'{spec.semantic_sha256}, not recorded {semantic_sha256}'
        )
    return spec


def _verify_model_calibration_result(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    result_id, spec_id, semantic_sha256 = key
    calibration = chain.repo('model_calibration')
    result = _require(
        calibration.get_result(result_id),
        f'model calibration result {result_id}',
    )
    if result.spec_id != spec_id:
        raise ValueError(
            f'model calibration result {result_id} binds spec '
            f'{result.spec_id}, not recorded {spec_id}'
        )
    if result.semantic_sha256 != semantic_sha256:
        raise ValueError(
            f'model calibration result {result_id} re-derives semantic hash '
            f'{result.semantic_sha256}, not recorded {semantic_sha256}'
        )
    _require(
        calibration.get_spec(spec_id),
        f'model calibration spec {spec_id} pinned by result {result_id}',
    )
    return result


def _verify_calibrated_model(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    materialized_model_id, calibration_result_id, semantic_sha256 = key
    calibration = chain.repo('model_calibration')
    model = _require(
        calibration.get_model(materialized_model_id),
        f'calibrated model {materialized_model_id}',
    )
    if model.calibration_result_id != calibration_result_id:
        raise ValueError(
            f'calibrated model {materialized_model_id} binds result '
            f'{model.calibration_result_id}, not recorded '
            f'{calibration_result_id}'
        )
    if model.semantic_sha256 != semantic_sha256:
        raise ValueError(
            f'calibrated model {materialized_model_id} re-derives semantic '
            f'hash {model.semantic_sha256}, not recorded {semantic_sha256}'
        )
    _require(
        calibration.get_result(calibration_result_id),
        f'model calibration result {calibration_result_id} pinned by '
        f'calibrated model {materialized_model_id}',
    )
    return model


def _verify_calibrated_model_freeze(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    freeze_id, calibration_result_id, semantic_sha256 = key
    calibration = chain.repo('model_calibration')
    freeze = _require(
        calibration.get_freeze(freeze_id),
        f'calibrated model freeze {freeze_id}',
    )
    if freeze.calibration_result_id != calibration_result_id:
        raise ValueError(
            f'calibrated model freeze {freeze_id} binds result '
            f'{freeze.calibration_result_id}, not recorded '
            f'{calibration_result_id}'
        )
    if freeze.semantic_sha256 != semantic_sha256:
        raise ValueError(
            f'calibrated model freeze {freeze_id} re-derives semantic hash '
            f'{freeze.semantic_sha256}, not recorded {semantic_sha256}'
        )
    _require(
        calibration.get_result(calibration_result_id),
        f'model calibration result {calibration_result_id} pinned by '
        f'freeze {freeze_id}',
    )
    return freeze


def _verify_holdout_record(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    record_id, freeze_id, semantic_sha256 = key
    calibration = chain.repo('model_calibration')
    record = _require(
        calibration.get_holdout_record(record_id),
        f'holdout discipline record {record_id}',
    )
    if record.freeze_id != freeze_id:
        raise ValueError(
            f'holdout discipline record {record_id} binds freeze '
            f'{record.freeze_id}, not recorded {freeze_id}'
        )
    if record.semantic_sha256 != semantic_sha256:
        raise ValueError(
            f'holdout discipline record {record_id} re-derives semantic '
            f'hash {record.semantic_sha256}, not recorded {semantic_sha256}'
        )
    _require(
        calibration.get_freeze(freeze_id),
        f'calibrated model freeze {freeze_id} pinned by holdout record '
        f'{record_id}',
    )
    return record


def _verify_calibration_evidence_event_ref(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    """Evidence-ledger rows resolve their freeze/holdout-record links."""
    record_id, freeze_id = key
    calibration = chain.repo('model_calibration')
    if freeze_id is not None:
        _require(
            calibration.get_freeze(freeze_id),
            f'calibrated model freeze {freeze_id} named by a calibration '
            'evidence event',
        )
    if record_id is not None:
        record = _require(
            calibration.get_holdout_record(record_id),
            f'holdout discipline record {record_id} named by a calibration '
            'evidence event',
        )
        if freeze_id is not None and record.freeze_id != freeze_id:
            raise ValueError(
                f'calibration evidence event binds record {record_id} of '
                f'freeze {record.freeze_id} beside freeze {freeze_id}'
            )
    return key


# Persisted authorities enumerated in dependency order. Each verify call is
# the domain repository's own fail-closed read path — no audit-side formula.
_REPLAY_PROBES: tuple[_ReplayProbe, ...] = (
    _ReplayProbe(
        'scene_revision',
        'scene_revisions',
        ('revision_id',),
        _get('scene', 'get'),
    ),
    _ReplayProbe(
        'constraint_workspace',
        'cad_constraint_workspaces',
        ('document_id',),
        _get('constraints', 'load'),
    ),
    _ReplayProbe(
        'search_spec',
        'cad_search_specs',
        ('search_spec_id',),
        _get('search', 'get'),
    ),
    _ReplayProbe(
        'system_variant',
        'cad_system_variants',
        ('variant_id',),
        _get('variant', 'get_variant'),
    ),
    _ReplayProbe(
        'system_variant_application',
        'cad_system_variant_applications',
        ('application_id',),
        _get('variant', 'get_application'),
    ),
    _ReplayProbe(
        'equipment_definition',
        'cad_equipment_definitions',
        ('semantic_sha256',),
        _get('equipment', 'get_definition_by_hash'),
    ),
    _ReplayProbe(
        'equipment_evidence',
        'cad_equipment_evidence_authorities',
        ('evidence_id',),
        _get('equipment', 'get_evidence'),
    ),
    _ReplayProbe(
        'directivity_dataset',
        'cad_directivity_datasets',
        ('dataset_id', 'version'),
        _get('directivity', 'get_dataset'),
    ),
    _ReplayProbe(
        'acoustic_treatment_definition',
        'cad_acoustic_treatment_definitions',
        ('definition_id', 'definition_version'),
        _get('treatment', 'get_definition'),
    ),
    _ReplayProbe(
        'acoustic_treatment_placement',
        'cad_acoustic_treatment_placements',
        ('instance_id',),
        _get('treatment', 'get_placement'),
    ),
    _ReplayProbe(
        'treatment_evidence',
        'cad_treatment_evidence_authorities',
        ('evidence_id',),
        _get('treatment', 'get_evidence'),
    ),
    _ReplayProbe(
        'coverage_scenario',
        'cad_coverage_scenarios',
        ('scenario_id',),
        _get('coverage', 'get_scenario'),
    ),
    _ReplayProbe(
        'coverage_evaluation',
        'cad_coverage_evaluations',
        ('evaluation_id',),
        _get('coverage', 'get_evaluation'),
    ),
    _ReplayProbe(
        'direct_level_scenario',
        'cad_direct_level_scenarios',
        ('scenario_id',),
        _get('direct_level', 'get_scenario'),
    ),
    _ReplayProbe(
        'direct_level_evaluation',
        'cad_direct_level_evaluations',
        ('evaluation_id',),
        _get('direct_level', 'get_evaluation'),
    ),
    _ReplayProbe(
        'amplifier_capability',
        'cad_amplifier_output_capabilities',
        ('semantic_sha256',),
        _get('headroom', 'get_amplifier_capability_by_hash'),
    ),
    _ReplayProbe(
        'speaker_electrical_load',
        'cad_speaker_electrical_loads',
        ('semantic_sha256',),
        _get('headroom', 'get_speaker_load_by_hash'),
    ),
    _ReplayProbe(
        'playback_chain_scenario',
        'cad_playback_chain_scenarios',
        ('scenario_id',),
        _get('headroom', 'get_scenario'),
    ),
    _ReplayProbe(
        'playback_chain_evaluation',
        'cad_playback_chain_evaluations',
        ('evaluation_id',),
        _get('headroom', 'get_evaluation'),
    ),
    _ReplayProbe(
        'standards_source_authority',
        'cad_standards_source_authorities',
        ('authority_id',),
        _get('standards', 'get_source_authority'),
    ),
    _ReplayProbe(
        'standards_observation_authority',
        'cad_standards_observation_authorities',
        ('authority_id',),
        _get('standards', 'get_observation_authority'),
    ),
    _ReplayProbe(
        'standards_profile',
        'cad_standards_profiles',
        ('profile_id', 'profile_version'),
        _get('standards', 'get_profile'),
    ),
    _ReplayProbe(
        'standards_evaluation',
        'cad_standards_evaluations',
        ('evaluation_id',),
        _get('standards', 'get_evaluation'),
    ),
    _ReplayProbe(
        'measurement',
        'cad_measurements',
        ('measurement_id',),
        _get('measurement', 'get_measurement'),
    ),
    _ReplayProbe(
        'measurement_dataset',
        'cad_frequency_responses',
        ('dataset_id',),
        _get('measurement', 'get_dataset'),
    ),
    _ReplayProbe(
        'measurement_ir_dataset',
        'cad_impulse_responses',
        ('dataset_id',),
        _get('measurement', 'get_ir_dataset'),
    ),
    _ReplayProbe(
        'measurement_plan',
        'cad_measurement_plans',
        ('plan_id', 'search_spec_id'),
        _verify_measurement_plan,
    ),
    _ReplayProbe(
        'measurement_lineage',
        'cad_measurement_lineage',
        ('lineage_id', 'document_id'),
        _verify_measurement_lineage,
    ),
    _ReplayProbe(
        'measurement_observation',
        'cad_measurement_observations',
        ('observation_id',),
        _get('quality', 'get_observation'),
    ),
    _ReplayProbe(
        'measurement_excitation_asset',
        'cad_excitation_assets',
        ('excitation_asset_id',),
        _get('quality', 'get_excitation_asset'),
    ),
    _ReplayProbe(
        'measurement_stimulus_profile',
        'cad_stimulus_profiles',
        ('stimulus_profile_id',),
        _get('quality', 'get_stimulus_profile'),
    ),
    _ReplayProbe(
        'measurement_timing_reference',
        'cad_timing_references',
        ('timing_reference_id',),
        _get('quality', 'get_timing_reference'),
    ),
    _ReplayProbe(
        'acoustic_level_calibration',
        'cad_acoustic_level_calibrations',
        ('calibration_id',),
        _get('quality', 'get_level_calibration'),
    ),
    _ReplayProbe(
        'dataset_level_reference',
        'cad_dataset_level_references',
        ('dataset_id',),
        _get('quality', 'get_dataset_level_reference'),
    ),
    _ReplayProbe(
        'routing_profile',
        'cad_routing_profiles',
        ('routing_profile_id',),
        _get('quality', 'get_routing_profile'),
    ),
    _ReplayProbe(
        'acquisition_context',
        'cad_acquisition_contexts',
        ('acquisition_context_id',),
        _get('quality', 'get_acquisition_context'),
    ),
    _ReplayProbe(
        'measurement_quality_report',
        'cad_measurement_quality_reports',
        ('report_id',),
        _get('quality', 'get_report'),
    ),
    _ReplayProbe(
        'wiring_check',
        'cad_wiring_checks',
        ('check_id',),
        _get('quality', 'get_wiring_check'),
    ),
    _ReplayProbe(
        'measurement_target_lineage',
        'cad_measurement_target_lineages',
        ('measurement_point_id',),
        _get('quality', 'get_target_lineage'),
    ),
    _ReplayProbe(
        'measurement_comparison',
        'cad_measurement_comparisons',
        ('comparison_id',),
        _get('measurement', 'get_comparison'),
    ),
    _ReplayProbe(
        'prediction_result',
        'cad_prediction_results',
        ('prediction_id',),
        _get('prediction', 'get'),
    ),
    _ReplayProbe(
        'roomsim_batch_spec',
        'cad_roomsim_batch_specs',
        ('batch_run_id',),
        _verify_roomsim_batch,
    ),
    _ReplayProbe(
        'roomsim_attempt',
        'cad_roomsim_candidate_attempts',
        ('attempt_id', 'batch_run_id'),
        _verify_roomsim_attempt,
    ),
    _ReplayProbe(
        'objective_evaluation',
        'cad_objective_evaluations',
        ('evaluation_id',),
        _verify_objective_evaluation,
    ),
    _ReplayProbe(
        'pareto_set',
        'cad_pareto_sets',
        ('pareto_set_id',),
        _get('objective', 'get_pareto_set'),
    ),
    _ReplayProbe(
        'model_validation',
        'cad_model_validations',
        ('validation_id',),
        _get('validation', 'get'),
    ),
    _ReplayProbe(
        'validation_campaign',
        'cad_validation_campaigns',
        ('campaign_id',),
        _get('campaign', 'get'),
    ),
    _ReplayProbe(
        'validation_campaign_registration',
        'cad_validation_campaign_registrations',
        ('registration_id',),
        _get('campaign', 'get_registration'),
    ),
    _ReplayProbe(
        'extended_parameter_evidence',
        'cad_extended_parameter_evidence',
        ('evidence_id',),
        _get('extended', 'get_parameter_evidence'),
    ),
    _ReplayProbe(
        'extended_model_capability',
        'cad_extended_model_capabilities',
        ('capability_id',),
        _get('extended', 'get_capability'),
    ),
    _ReplayProbe(
        'extended_search_spec',
        'cad_extended_search_specs',
        ('extended_search_id',),
        _get('extended', 'get_spec'),
    ),
    _ReplayProbe(
        'adaptive_plan',
        'cad_adaptive_plans',
        ('plan_id',),
        _get('adaptive', 'get'),
    ),
    _ReplayProbe(
        'adaptive_extended_observation',
        'cad_adaptive_extended_observations',
        ('observation_id',),
        _get('adaptive_extended', 'get_observation'),
    ),
    _ReplayProbe(
        'adaptive_extended_plan',
        'cad_adaptive_extended_plans',
        ('plan_id',),
        _get('adaptive_extended', 'get_plan'),
    ),
    _ReplayProbe(
        'robustness_spec',
        'cad_robustness_specs',
        ('robustness_spec_id',),
        _get('robustness', 'get_spec'),
    ),
    _ReplayProbe(
        'robustness_sample',
        'cad_perturbation_samples',
        ('sample_id', 'robustness_spec_id'),
        _verify_robustness_sample,
    ),
    _ReplayProbe(
        'robustness_evaluation',
        'cad_robustness_evaluations',
        ('evaluation_id', 'robustness_spec_id'),
        _verify_robustness_evaluation,
    ),
    _ReplayProbe(
        'robustness_validation_case',
        'cad_robustness_validation_cases',
        ('case_id',),
        _get('robustness_validation', 'get_case'),
    ),
    _ReplayProbe(
        'robustness_validation_decision',
        'cad_robustness_validation_decisions',
        ('decision_id',),
        _get('robustness_validation', 'get_decision'),
    ),
    _ReplayProbe(
        'joint_optimization_spec',
        'cad_joint_optimization_specs',
        ('spec_id',),
        _get('joint', 'get_spec'),
    ),
    _ReplayProbe(
        'joint_candidate',
        'cad_joint_candidates',
        ('candidate_id',),
        _get('joint', 'get_candidate'),
    ),
    _ReplayProbe(
        'joint_candidate_evaluation',
        'cad_joint_candidate_evaluations',
        ('evaluation_binding_id',),
        _get('joint', 'get_evaluation'),
    ),
    _ReplayProbe(
        'joint_candidate_selection',
        'cad_joint_candidate_selections',
        ('selection_id',),
        _get('joint', 'get_selection'),
    ),
    _ReplayProbe(
        'topology_comparison_spec',
        'cad_topology_comparison_specs',
        ('comparison_id',),
        _get('topology', 'get_spec'),
    ),
    _ReplayProbe(
        'topology_comparison_bundle',
        'cad_topology_comparison_bundles',
        ('bundle_id',),
        _get('topology', 'get_bundle'),
    ),
    _ReplayProbe(
        'topology_comparison_evaluation',
        'cad_topology_comparison_evaluations',
        ('evaluation_id',),
        _get('topology', 'get_evaluation'),
    ),
    _ReplayProbe(
        'topology_comparison_selection',
        'cad_topology_comparison_selections',
        ('selection_id',),
        _get('topology', 'get_selection'),
    ),
    _ReplayProbe(
        'calibration_plan',
        'cad_calibration_plans',
        ('plan_id',),
        _get('calibration', 'get_plan'),
    ),
    _ReplayProbe(
        'calibration_export',
        'cad_calibration_exports',
        ('export_id',),
        _get('calibration', 'get_export'),
    ),
    _ReplayProbe(
        'calibration_verification_plan',
        'cad_calibration_verification_plans',
        ('verification_plan_id',),
        _get('calibration', 'get_verification_plan'),
    ),
    _ReplayProbe(
        'calibration_verification_registration',
        'cad_calibration_verification_registrations',
        ('registration_id',),
        _get('calibration', 'get_verification_plan_registration'),
    ),
    _ReplayProbe(
        'calibration_verification_completion',
        'cad_calibration_verification_completions',
        ('completion_id',),
        _get('calibration', 'get_verification_completion'),
    ),
    _ReplayProbe(
        'model_calibration_spec',
        'cad_calibration_specs',
        ('spec_id', 'semantic_sha256'),
        _verify_model_calibration_spec,
    ),
    _ReplayProbe(
        'model_calibration_result',
        'cad_calibration_results',
        ('result_id', 'spec_id', 'semantic_sha256'),
        _verify_model_calibration_result,
    ),
    _ReplayProbe(
        'calibrated_model',
        'cad_calibration_models',
        ('materialized_model_id', 'calibration_result_id', 'semantic_sha256'),
        _verify_calibrated_model,
    ),
    _ReplayProbe(
        'calibrated_model_freeze',
        'cad_calibration_freezes',
        ('freeze_id', 'calibration_result_id', 'semantic_sha256'),
        _verify_calibrated_model_freeze,
    ),
    _ReplayProbe(
        'holdout_discipline_record',
        'cad_calibration_holdout_records',
        ('record_id', 'freeze_id', 'semantic_sha256'),
        _verify_holdout_record,
    ),
    _ReplayProbe(
        'calibration_evidence_event',
        'cad_calibration_evidence_events',
        ('record_id', 'freeze_id'),
        _verify_calibration_evidence_event_ref,
    ),
    _ReplayProbe(
        'system_variant_as_built',
        'cad_system_variant_as_built',
        ('record_id',),
        _get('lifecycle', 'get'),
    ),
    _ReplayProbe(
        'system_variant_measured',
        'cad_system_variant_measured',
        ('record_id',),
        _get('measured', 'get'),
    ),
    _ReplayProbe(
        'system_variant_measurement_plan',
        'cad_system_variant_measurement_plans',
        ('plan_id',),
        _get('measurement_campaign', 'get_plan'),
    ),
    _ReplayProbe(
        'system_variant_measurement_campaign',
        'cad_system_variant_measurement_campaigns',
        ('campaign_id',),
        _get('measurement_campaign', 'get_campaign'),
    ),
    _ReplayProbe(
        'system_variant_measurement_registration',
        'cad_system_variant_measurement_campaign_registrations',
        ('registration_id',),
        _get('measurement_campaign', 'get_campaign_registration'),
    ),
    _ReplayProbe(
        'system_variant_measurement_plan_completion',
        'cad_system_variant_measurement_plan_completions',
        ('completion_id',),
        _get('measurement_campaign', 'get_plan_completion'),
    ),
    _ReplayProbe(
        'system_variant_measurement_campaign_completion',
        'cad_system_variant_measurement_campaign_completions',
        ('completion_id',),
        _get('measurement_campaign', 'get_campaign_completion'),
    ),
    _ReplayProbe(
        'r110_compiled_source_model',
        'cad_r110_compiled_source_models',
        ('semantic_sha256',),
        _get('r110', 'get_model'),
    ),
    _ReplayProbe(
        'r120_compiled_geometry',
        'cad_r120_compiled_geometry',
        ('compiled_hash_sha256',),
        _get('r120', 'get_compiled_geometry_by_hash'),
    ),
    _ReplayProbe(
        'r120_leak_portal_diagnostic',
        'cad_r120_leak_portal_diagnostics',
        ('diagnostic_result_id',),
        _get('r120', 'get_leak_portal_diagnostic'),
    ),
    _ReplayProbe(
        'treatment_boundary_overlay',
        'cad_treatment_boundary_overlays',
        ('overlay_id',),
        _get('overlay', 'get_overlay'),
    ),
    _ReplayProbe(
        'treatment_boundary_composition',
        'cad_treatment_boundary_compositions',
        ('composition_id',),
        _get('overlay', 'get_composition'),
    ),
    _ReplayProbe(
        'wave_excitation_evidence',
        'cad_wave_excitation_evidence_authorities',
        ('evidence_id',),
        _get('wave', 'get_evidence'),
    ),
    _ReplayProbe(
        'wave_excitation',
        'cad_acoustic_wave_excitations',
        ('excitation_id',),
        _get('wave', 'get_excitation'),
    ),
    _ReplayProbe(
        'wave_source_excitation_binding',
        'cad_wave_source_excitation_bindings',
        ('binding_id',),
        _get('wave', 'get_binding'),
    ),
    _ReplayProbe(
        'acoustic_scene_snapshot',
        'cad_acoustic_scene_snapshots',
        ('snapshot_id',),
        _get('snapshot', 'get_snapshot'),
    ),
    _ReplayProbe(
        'acoustic_prediction_request',
        'cad_acoustic_prediction_requests',
        ('request_id',),
        _get('snapshot', 'get_prediction_request'),
    ),
    _ReplayProbe(
        'projector_specification',
        'cad_projector_specifications',
        ('specification_id', 'version'),
        _get('video', 'get_projector_specification'),
    ),
    _ReplayProbe(
        'projector_spec_evidence',
        'cad_projector_spec_evidence',
        ('evidence_sha256',),
        _get('video', 'get_projector_spec_evidence'),
    ),
    _ReplayProbe(
        'video_geometry_evaluation',
        'cad_video_geometry_evaluations',
        ('evaluation_id',),
        _get('video', 'get_evaluation'),
    ),
    _ReplayProbe(
        'applicability_attestation',
        'cad_applicability_attestations',
        ('attestation_id',),
        _get('applicability', 'get'),
    ),
    _ReplayProbe(
        'scene_document_head',
        'scene_document_heads',
        ('document_id',),
        _verify_scene_head,
    ),
    _ReplayProbe(
        'authoring_constraint_set',
        'authoring_constraint_sets',
        ('document_id',),
        _verify_constraint_head,
    ),
    _ReplayProbe(
        'authoring_constraint_revision',
        'authoring_constraint_revisions',
        ('constraint_revision_id', 'document_id'),
        _verify_constraint_revision,
    ),
    _ReplayProbe(
        'measurement_disposition',
        'cad_measurement_dispositions',
        ('disposition_id', 'measurement_id'),
        _verify_measurement_disposition,
    ),
    _ReplayProbe(
        'measurement_correction',
        'cad_measurement_corrections',
        ('correction_id', 'measurement_id'),
        _verify_measurement_correction,
    ),
    # ---- #718 hardening families ---------------------------------------
    _ReplayProbe(
        'design_comparison_set',
        'cad_design_comparison_sets',
        ('set_id', 'document_id'),
        _verify_comparison_set,
    ),
    _ReplayProbe(
        'operating_preset',
        'cad_operating_presets',
        ('preset_id',),
        _get('presets', 'verify_persisted_preset'),
    ),
    _ReplayProbe(
        'applied_preset_state',
        'cad_applied_preset_states',
        ('applied_id', 'preset_id'),
        _verify_applied_preset,
    ),
    _ReplayProbe(
        'preset_measurement_binding',
        'cad_preset_measurement_bindings',
        ('binding_id', 'preset_id'),
        _verify_preset_binding,
    ),
    _ReplayProbe(
        'health_baseline',
        'cad_health_baselines',
        ('baseline_id',),
        _get('health', 'verify_persisted_baseline'),
    ),
    _ReplayProbe(
        'health_check_plan',
        'cad_health_check_plans',
        ('plan_id', 'baseline_id'),
        _verify_health_plan,
    ),
    _ReplayProbe(
        'health_check_run',
        'cad_health_check_runs',
        ('run_id', 'plan_id'),
        _verify_health_run,
    ),
    _ReplayProbe(
        'constraint_snapshot',
        'cad_constraint_snapshots',
        ('snapshot_id',),
        _get('checkpoints', 'verify_persisted_snapshot'),
    ),
    _ReplayProbe(
        'design_checkpoint',
        'cad_design_checkpoints',
        ('checkpoint_id',),
        _get('checkpoints', 'verify_persisted_checkpoint'),
    ),
    _ReplayProbe(
        'checkpoint_restore',
        'cad_checkpoint_restores',
        ('restore_id', 'checkpoint_id'),
        _verify_checkpoint_restore,
    ),
    _ReplayProbe(
        'project_registry',
        'htdt_project_documents',
        ('project_id',),
        _get('project_library', 'verify_project_registration'),
    ),
    _ReplayProbe(
        'design_decision',
        'design_decisions',
        ('decision_id', 'document_id'),
        _verify_design_decision,
    ),
    _ReplayProbe(
        'design_brief',
        'cad_design_briefs',
        ('brief_id', 'document_id'),
        _verify_design_brief,
    ),
    _ReplayProbe(
        'analysis_study',
        'cad_analysis_studies',
        ('study_id', 'document_id'),
        _verify_analysis_study,
    ),
    _ReplayProbe(
        'assumption_decision',
        'assumption_decisions',
        ('decision_id', 'document_id'),
        _verify_assumption_decision,
    ),
    _ReplayProbe(
        'external_dependency',
        'cad_external_dependencies',
        ('dependency_id', 'document_id'),
        _verify_external_dependency,
    ),
    _ReplayProbe(
        'dependency_resolution_event',
        'cad_dependency_resolution_events',
        ('event_id', 'dependency_id'),
        _verify_dependency_event,
    ),
    _ReplayProbe(
        'equipment_upgrade',
        'cad_equipment_upgrades',
        ('upgrade_id',),
        _get('upgrades', 'get_upgrade'),
    ),
    _ReplayProbe(
        'upgrade_adoption',
        'cad_upgrade_adoptions',
        ('adoption_id', 'document_id'),
        _verify_upgrade_adoption,
    ),
    _ReplayProbe(
        'equipment_binding_semantics',
        'cad_equipment_binding_semantics',
        ('binding_id',),
        _get('equipment_bindings', 'get_binding'),
    ),
    _ReplayProbe(
        'installed_equipment_instance',
        'cad_installed_equipment_instances',
        ('instance_id',),
        _get('installed', 'get_instance'),
    ),
    _ReplayProbe(
        'installed_definition_binding',
        'cad_installed_definition_bindings',
        ('binding_id', 'instance_id'),
        _verify_installed_binding,
    ),
    _ReplayProbe(
        'installed_device_observation',
        'cad_installed_device_observations',
        ('observation_id', 'instance_id'),
        _verify_installed_observation,
    ),
    _ReplayProbe(
        'installed_equipment_replacement',
        'cad_installed_equipment_replacements',
        ('replacement_id', 'document_id'),
        _verify_installed_replacement,
    ),
    _ReplayProbe(
        'measurement_runner_plan',
        'cad_measurement_runner_plans',
        ('plan_id',),
        _get('runner', 'get_plan'),
    ),
    _ReplayProbe(
        'measurement_runner_run',
        'cad_measurement_runner_runs',
        ('run_id', 'plan_id'),
        _verify_runner_run,
    ),
    _ReplayProbe(
        'measurement_runner_event',
        'cad_measurement_runner_events',
        ('event_id', 'run_id'),
        _verify_runner_event,
    ),
    _ReplayProbe(
        'project_tombstone',
        'htdt_project_tombstones',
        ('tombstone_id', 'project_id'),
        _verify_project_tombstone,
    ),
    _ReplayProbe(
        'field_explorer_session',
        'cad_field_explorer_sessions',
        ('session_id',),
        _get('field_explorer', 'get'),
    ),
    _ReplayProbe(
        'model_calibration_spec',
        'cad_calibration_specs',
        ('spec_id',),
        _get('model_calibration', 'get_spec'),
    ),
    _ReplayProbe(
        'model_calibration_result',
        'cad_calibration_results',
        ('result_id', 'spec_id'),
        _verify_calibration_result,
    ),
    _ReplayProbe(
        'model_calibration_model',
        'cad_calibration_models',
        ('materialized_model_id', 'calibration_result_id'),
        _verify_calibration_model,
    ),
    _ReplayProbe(
        'model_calibration_freeze',
        'cad_calibration_freezes',
        ('freeze_id', 'calibration_result_id'),
        _verify_calibration_freeze,
    ),
    _ReplayProbe(
        'model_calibration_holdout_record',
        'cad_calibration_holdout_records',
        ('record_id', 'freeze_id'),
        _verify_calibration_holdout,
    ),
    _ReplayProbe(
        'model_calibration_evidence_event',
        'cad_calibration_evidence_events',
        (
            'seq',
            'campaign_id',
            'campaign_sha256',
            'consumption_kind',
            'freeze_id',
            'record_id',
        ),
        _verify_calibration_evidence_event,
    ),
)

# Managed-asset manifest/evidence tables: every row must resolve to the
# retained content-addressed file with matching digest (and length when
# recorded). ``path_column`` is the archive-style relative path when the
# table records one; otherwise the file lives at
# ``measurement-assets/<digest>``. ``where`` narrows to file-backed rows
# (manual/external evidence kinds never carry retained bytes).
_ASSET_TABLES: tuple[
    tuple[str, str, str | None, str | None, str | None], ...
] = (
    # (table, sha column, size column, path column, where clause)
    ('cad_measurement_assets', 'sha256', 'size_bytes', 'relative_path', None),
    (
        'cad_quality_calibration_files',
        'sha256',
        'size_bytes',
        'relative_path',
        None,
    ),
    ('cad_directivity_source_assets', 'source_asset_sha256', None, None, None),
    (
        'cad_wave_excitation_source_assets',
        'source_asset_sha256',
        None,
        None,
        None,
    ),
    (
        'cad_projector_spec_source_assets',
        'source_asset_sha256',
        None,
        None,
        None,
    ),
    (
        'cad_equipment_evidence_authorities',
        'source_sha256',
        None,
        None,
        "authority_kind='managed_source_asset'",
    ),
    (
        'cad_treatment_evidence_authorities',
        'source_sha256',
        None,
        None,
        'source_sha256 IS NOT NULL',
    ),
)

_REPLAY_TABLES = frozenset(probe.table for probe in _REPLAY_PROBES)

#: Explicit coverage policy for every persistent table that carries no
#: replay probe and no managed-bytes check — each entry is
#: ``(coverage class, bounded reason)``. Classes:
#:
#: - ``STRUCTURAL_ONLY``: rows carry semantic payload claims but no
#:   canonical replay path exists yet; the strongest available
#:   verification is schema + JSON-payload parse. The reason records why
#:   no replay exists — "JSON parses" alone is not a semantic audit.
#: - ``OPERATIONAL_METADATA``: bookkeeping/editor/derived state with no
#:   independent semantic claim (schema versions, head pointers, editor
#:   payloads, ingestion bookkeeping).
#: - ``EPHEMERAL``: transient state that may be stale by design.
#:
#: A persistent table absent from this registry and from the replay/asset
#: registries is reported UNCLASSIFIED and fails the audit: coverage is
#: fail-closed, never inferred from parseability.
_TABLE_POLICY: dict[str, tuple[str, str]] = {
    'sqlite_sequence': (
        'OPERATIONAL_METADATA',
        'sqlite autoincrement bookkeeping',
    ),
    'htdt_storage_gc_pending': (
        'pending blob-GC queue — transient operational state re-derivable '
        'from the blob store'
    ),
    'ci_marker': (
        'OPERATIONAL_METADATA',
        'scratch table the packaged-build CI pipeline writes into the live '
        'database to prove backup/restore round-trips carry non-HTDT rows; '
        'it stores no HTDT authority',
    ),
    'native_schema_metadata': (
        'OPERATIONAL_METADATA',
        'schema version bookkeeping, not user authority',
    ),
    'native_schema_migrations': (
        'OPERATIONAL_METADATA',
        'migration history bookkeeping',
    ),
    'scene_recovery_snapshots': (
        'OPERATIONAL_METADATA',
        'crash-recovery payload replaced by the next save; not an '
        'authoritative derivation',
    ),
    'editor_camera_states': (
        'OPERATIONAL_METADATA',
        'editor camera payload — viewport convenience, not design authority',
    ),
    'editor_named_views': (
        'OPERATIONAL_METADATA',
        'editor named-view payloads — presentation convenience, not '
        'design authority',
    ),
    'editor_view_states': (
        'OPERATIONAL_METADATA',
        'editor view-state payloads — presentation convenience, not '
        'design authority',
    ),
    'floor_plan_underlays': (
        'OPERATIONAL_METADATA',
        'editor floor-plan underlay payloads — UI convenience, not '
        'design authority',
    ),
    'seating_layout_specs': (
        'OPERATIONAL_METADATA',
        'editor seating-layout payloads — UI convenience, not design '
        'authority',
    ),
    'capture_bundles': (
        'OPERATIONAL_METADATA',
        'capture ingestion staging-bundle bookkeeping',
    ),
    'capture_revisions': (
        'OPERATIONAL_METADATA',
        'capture ingestion revision bookkeeping',
    ),
    'capture_roomplan_records': (
        'OPERATIONAL_METADATA',
        'capture ingestion room-plan bookkeeping',
    ),
    'capture_source_evidence': (
        'OPERATIONAL_METADATA',
        'capture ingestion source-evidence bookkeeping',
    ),
    'capture_revision_conflicts': (
        'OPERATIONAL_METADATA',
        'capture ingestion conflict bookkeeping',
    ),
    'capture_raw_visual_mesh_bindings': (
        'OPERATIONAL_METADATA',
        'capture ingestion raw-mesh binding bookkeeping',
    ),
    'capture_coordinate_authorities': (
        'STRUCTURAL_ONLY',
        'coordinate-authority linkage claims; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_authority_records': (
        'STRUCTURAL_ONLY',
        'capture authority linkage claims; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'capture_ingestion_authority_links': (
        'STRUCTURAL_ONLY',
        'ingestion authority-link claims; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'capture_ingestion_mesh_links': (
        'STRUCTURAL_ONLY',
        'ingestion mesh-link claims; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'capture_ingestion_source_links': (
        'STRUCTURAL_ONLY',
        'ingestion source-link claims; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'capture_ingestion_lineages': (
        'STRUCTURAL_ONLY',
        'ingestion lineage claims retained across deletion; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_amplifier_electrical_limits': (
        'STRUCTURAL_ONLY',
        'electrical-limit payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_frequency_resolved_evaluations': (
        'STRUCTURAL_ONLY',
        'frequency-resolved evaluation payload authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_installation_contexts': (
        'STRUCTURAL_ONLY',
        'installation-context payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_measurement_attachments': (
        'STRUCTURAL_ONLY',
        'measurement attachment payload authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'cad_r120_compile_inputs': (
        'STRUCTURAL_ONLY',
        'compile-input record; derivations are re-verified via the '
        'compiled-geometry replay probe',
    ),
    'cad_r120_leak_diagnostic_inputs': (
        'STRUCTURAL_ONLY',
        'leak-diagnostic input record; derivations are re-verified via '
        'the leak-diagnostic replay probe',
    ),
    'cad_raw_mesh_repair_bundles': (
        'STRUCTURAL_ONLY',
        'mesh-repair bundle payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_speaker_impedances': (
        'STRUCTURAL_ONLY',
        'speaker impedance payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_calibration_lifecycle_events': (
        'OPERATIONAL_METADATA',
        'append-only lifecycle event log — operational bookkeeping',
    ),
    # ---- operational bookkeeping -------------------------------------
    'cad_reconciliation_decisions': (
        'OPERATIONAL_METADATA',
        'append-only reconciliation decision log — operational bookkeeping',
    ),
    'cad_project_notes': (
        'OPERATIONAL_METADATA',
        'operator-entered free-text notes — no authority claims',
    ),
    'capture_inbox_items': (
        'OPERATIONAL_METADATA',
        'capture inbox triage/disposition bookkeeping — operational '
        'intake state, not canonical authority',
    ),
    'capture_inbox_promotions': (
        'OPERATIONAL_METADATA',
        'capture inbox promotion log — operational bookkeeping',
    ),
    'capture_inbox_registrations': (
        'OPERATIONAL_METADATA',
        'capture inbox registration bookkeeping — operational intake '
        'alignment record',
    ),
    'capture_inbox_supersessions': (
        'OPERATIONAL_METADATA',
        'capture inbox supersession bookkeeping — operational intake '
        'replacement record',
    ),
    'scene_revision_labels': (
        'OPERATIONAL_METADATA',
        'editor-provided revision labels — display metadata, not '
        'authority',
    ),
    # ---- payload authority, replay pending ----------------------------
    'cad_acoustic_treatment_comparisons': (
        'STRUCTURAL_ONLY',
        'acoustic treatment comparison authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_treatment_comparison_outcomes': (
        'STRUCTURAL_ONLY',
        'treatment comparison outcome authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_ambient_comparisons': (
        'STRUCTURAL_ONLY',
        'ambient comparison authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_ambient_conditions': (
        'STRUCTURAL_ONLY',
        'ambient condition authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_ambient_criteria': (
        'STRUCTURAL_ONLY',
        'ambient criteria authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_ambient_evaluations': (
        'STRUCTURAL_ONLY',
        'ambient evaluation authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_ambient_profiles': (
        'STRUCTURAL_ONLY',
        'ambient profile authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_applied_settings': (
        'STRUCTURAL_ONLY',
        'applied-settings authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_av_latency_measurements': (
        'STRUCTURAL_ONLY',
        'AV latency measurement authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_av_sync_conditions': (
        'STRUCTURAL_ONLY',
        'AV sync condition authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_cable_runs': (
        'STRUCTURAL_ONLY',
        'cable run authority; canonical replay path pending — strongest '
        'verification is schema + payload parse',
    ),
    'cad_commissioning_plans': (
        'STRUCTURAL_ONLY',
        'commissioning plan authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_commissioning_runs': (
        'STRUCTURAL_ONLY',
        'commissioning run authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_cost_evaluations': (
        'STRUCTURAL_ONLY',
        'cost evaluation authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_cost_records': (
        'STRUCTURAL_ONLY',
        'cost record authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_direct_view_evaluations': (
        'STRUCTURAL_ONLY',
        'direct-view evaluation authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_direct_view_specifications': (
        'STRUCTURAL_ONLY',
        'direct-view specification authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_evidence_observations': (
        'STRUCTURAL_ONLY',
        'evidence observation authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_evidence_subjects': (
        'STRUCTURAL_ONLY',
        'evidence subject authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_field_evidence': (
        'STRUCTURAL_ONLY',
        'field evidence authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_field_evidence_targets': (
        'STRUCTURAL_ONLY',
        'field evidence target authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_gain_structure_evaluations': (
        'STRUCTURAL_ONLY',
        'gain-structure evaluation authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_gain_structure_scenarios': (
        'STRUCTURAL_ONLY',
        'gain-structure scenario authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_installation_datums': (
        'STRUCTURAL_ONLY',
        'installation datum authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_ir_analysis_results': (
        'STRUCTURAL_ONLY',
        'IR analysis result authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_ir_analysis_specs': (
        'STRUCTURAL_ONLY',
        'IR analysis spec authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_layout_profiles': (
        'STRUCTURAL_ONLY',
        'layout profile authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_line_level_stages': (
        'STRUCTURAL_ONLY',
        'line-level stage authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_materialized_pattern_points': (
        'STRUCTURAL_ONLY',
        'materialized pattern point authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_measurement_target_patterns': (
        'STRUCTURAL_ONLY',
        'measurement target pattern authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_multi_seat_results': (
        'STRUCTURAL_ONLY',
        'multi-seat result authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_multi_seat_sets': (
        'STRUCTURAL_ONLY',
        'multi-seat set authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_plan_target_bindings': (
        'STRUCTURAL_ONLY',
        'plan target binding authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_r140_gpu_authorities': (
        'STRUCTURAL_ONLY',
        'R140 GPU authority; canonical replay path pending — strongest '
        'verification is schema + payload parse',
    ),
    'cad_room_operating_states': (
        'STRUCTURAL_ONLY',
        'room operating state authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_seat_priority_profiles': (
        'STRUCTURAL_ONLY',
        'seat priority profile authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_source_response_selections': (
        'STRUCTURAL_ONLY',
        'source response selection authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_source_responses': (
        'STRUCTURAL_ONLY',
        'source response payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_target_curve_profiles': (
        'STRUCTURAL_ONLY',
        'target curve profile authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_tolerance_profiles': (
        'STRUCTURAL_ONLY',
        'tolerance profile authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'capture_connected_space_documents': (
        'STRUCTURAL_ONLY',
        'connected space document authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'physical_space_models': (
        'STRUCTURAL_ONLY',
        'physical space model authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_topology_spaces': (
        'STRUCTURAL_ONLY',
        'topology search space authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_topology_space_options': (
        'STRUCTURAL_ONLY',
        'topology space option binding authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_topology_search_specs': (
        'STRUCTURAL_ONLY',
        'topology search spec authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_topology_placement_candidates': (
        'STRUCTURAL_ONLY',
        'topology placement candidate authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_topology_candidate_variants': (
        'STRUCTURAL_ONLY',
        'topology candidate variant binding authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    # ---- further payload authorities, replay pending ------------------
    'field_return_contributions': (
        'STRUCTURAL_ONLY',
        'field return contribution authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'project_action_items': (
        'STRUCTURAL_ONLY',
        'project action item authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'project_templates': (
        'STRUCTURAL_ONLY',
        'project template authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'template_instantiations': (
        'STRUCTURAL_ONLY',
        'template instantiation authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'r150_path_frequency_response_artifacts': (
        'STRUCTURAL_ONLY',
        'path frequency response artifact authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'r160_numerical_hybrid_responses': (
        'STRUCTURAL_ONLY',
        'numerical hybrid response authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_acoustic_materials': (
        'STRUCTURAL_ONLY',
        'acoustic material authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_acoustic_solver_adapters': (
        'STRUCTURAL_ONLY',
        'acoustic solver adapter authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_acoustic_solver_dispatch_bindings': (
        'STRUCTURAL_ONLY',
        'acoustic solver dispatch binding authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_acoustic_solver_results': (
        'STRUCTURAL_ONLY',
        'acoustic solver result authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_current_topologies': (
        'STRUCTURAL_ONLY',
        'current topology authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_deterministic_ga_execution_inputs': (
        'STRUCTURAL_ONLY',
        'deterministic GA execution input authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_deterministic_path_artifacts': (
        'STRUCTURAL_ONLY',
        'deterministic path artifact authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_environment_profiles': (
        'STRUCTURAL_ONLY',
        'environment profile authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_environment_selections': (
        'STRUCTURAL_ONLY',
        'environment selection authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_hybrid_acoustic_results': (
        'STRUCTURAL_ONLY',
        'hybrid acoustic result authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_hybrid_prediction_provider_bindings': (
        'STRUCTURAL_ONLY',
        'hybrid prediction provider binding authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_hybrid_prediction_provider_objectives': (
        'STRUCTURAL_ONLY',
        'hybrid prediction provider objective authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_hybrid_prediction_providers': (
        'STRUCTURAL_ONLY',
        'hybrid prediction provider authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_hybrid_stitching_policies': (
        'STRUCTURAL_ONLY',
        'hybrid stitching policy authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_intervention_alternatives': (
        'STRUCTURAL_ONLY',
        'intervention alternative authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_intervention_study_specs': (
        'STRUCTURAL_ONLY',
        'intervention study spec authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_listener_pose_selections': (
        'STRUCTURAL_ONLY',
        'listener pose selection authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_listener_poses': (
        'STRUCTURAL_ONLY',
        'listener pose authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_multifidelity_finalizations': (
        'STRUCTURAL_ONLY',
        'multifidelity finalization authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_multifidelity_plans': (
        'STRUCTURAL_ONLY',
        'multifidelity plan authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_multifidelity_screening_evaluations': (
        'STRUCTURAL_ONLY',
        'multifidelity screening evaluation authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_multifidelity_stage_results': (
        'STRUCTURAL_ONLY',
        'multifidelity stage result authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_o90_robust_pareto_evaluations': (
        'STRUCTURAL_ONLY',
        'robust Pareto evaluation authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_prediction_matrix_specs': (
        'STRUCTURAL_ONLY',
        'prediction matrix spec authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_prediction_matrix_result_sets': (
        'STRUCTURAL_ONLY',
        'prediction matrix result-set authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_prediction_matrix_runs': (
        'STRUCTURAL_ONLY',
        'prediction matrix run authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_prediction_provider_bindings': (
        'STRUCTURAL_ONLY',
        'prediction provider binding authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_prediction_provider_objectives': (
        'STRUCTURAL_ONLY',
        'prediction provider objective authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_prediction_providers': (
        'STRUCTURAL_ONLY',
        'prediction provider authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_proposal_objective_result_authorities': (
        'STRUCTURAL_ONLY',
        'proposal objective result authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_proposal_perturbation_samples': (
        'STRUCTURAL_ONLY',
        'proposal perturbation sample authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_proposal_robust_pareto_evaluations': (
        'STRUCTURAL_ONLY',
        'proposal robust Pareto evaluation authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_proposal_robustness_evaluations': (
        'STRUCTURAL_ONLY',
        'proposal robustness evaluation authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
    ),
    'cad_proposal_robustness_specs': (
        'STRUCTURAL_ONLY',
        'proposal robustness spec authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_r140_execution_attempts': (
        'STRUCTURAL_ONLY',
        'R140 execution attempt authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_r140_execution_cache': (
        'STRUCTURAL_ONLY',
        'R140 execution cache authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_r140_execution_results': (
        'STRUCTURAL_ONLY',
        'R140 execution result authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_r140_execution_schedules': (
        'STRUCTURAL_ONLY',
        'R140 execution schedule authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_r140_execution_tasks': (
        'STRUCTURAL_ONLY',
        'R140 execution task authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_r140_resource_estimates': (
        'STRUCTURAL_ONLY',
        'R140 resource estimate authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_screen_transfer_selections': (
        'STRUCTURAL_ONLY',
        'screen transfer selection authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_screen_transfers': (
        'STRUCTURAL_ONLY',
        'screen transfer authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_surface_material_assignments': (
        'STRUCTURAL_ONLY',
        'surface material assignment authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_video_geometry_workspaces': (
        'STRUCTURAL_ONLY',
        'video geometry workspace authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_mesh_compositions': (
        'STRUCTURAL_ONLY',
        'capture mesh composition authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_mission_packages': (
        'STRUCTURAL_ONLY',
        'capture mission package authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_receiver_config': (
        'STRUCTURAL_ONLY',
        'capture receiver config authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_receiver_deliveries': (
        'STRUCTURAL_ONLY',
        'capture receiver delivery authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_receiver_pairings': (
        'STRUCTURAL_ONLY',
        'capture receiver pairing authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'capture_semantic_promotions': (
        'STRUCTURAL_ONLY',
        'capture semantic promotion authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'htdt_project_imports': (
        'OPERATIONAL_METADATA',
        'project import bookkeeping — the registry and tombstones are the '
        'persisted authority, import rows only record the operation',
    ),
    'htdt_legacy_imports': (
        'OPERATIONAL_METADATA',
        'legacy-data migration bookkeeping — records which legacy '
        'projects already migrated into the native registry',
    ),
    'htdt_storage_gc_pending': (
        'OPERATIONAL_METADATA',
        'storage-GC work queue — pending deletions are re-derived from '
        'scan state and carry no semantic authority',
    ),
    'projects': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'contexts': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'sessions': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'constraint_sets': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'search_specs': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'assets': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'measurements': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'datasets': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'comparisons': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'asset_links': (
        'STRUCTURAL_ONLY',
        'legacy generic store — superseded by cad_* domain authorities; '
        'strongest verification is schema + payload parse',
    ),
    'metadata': (
        'STRUCTURAL_ONLY',
        'legacy schema-marker table — superseded by cad_* domain '
        'authorities; strongest verification is schema + payload parse',
    ),    'cad_acoustic_source_poses': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_bass_management_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_bass_management_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_evidence_events': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_freezes': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_holdout_records': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_lifecycle_events': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_models': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_results': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_calibration_specs': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_compute_benchmarks': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_data_source_registry': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_dataset_reviews': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_device_action_acks': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_device_capability_snapshots': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_device_target_bindings': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_field_evidence_records': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_field_sessions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_importer_declarations': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_material_definitions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_material_evidence': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_measurement_pose_observations': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_observed_device_states': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_planned_observed_deltas': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_playback_level_conditions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_project_notes': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_proposed_device_actions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_raw_source_records': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_reconciliation_decisions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_reference_playback_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_review_notes': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_site_relationships': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_site_spaces': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_source_review_decisions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_speaker_datasets': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_speaker_definitions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_upstream_version_candidates': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_validation_benchmark_specs': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_validation_cases': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_validation_corpus_entries': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_presentation_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_presentation_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_signal_paths': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_signal_path_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_lighting_scenes': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_lighting_scene_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_tactile_actuator_definitions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_tactile_processing_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_tactile_profile_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_usable_output_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_usable_output_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_photometric_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_photometric_profile_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_screen_optical_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_screen_optical_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_color_target_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_color_target_selections': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_color_measurement_sets': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_ambient_reflectance_profiles': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_visual_qa_verdicts': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'capture_bundles': (
        'STRUCTURAL_ONLY',
        'capture-pipeline record — covered by canonical ingestion-run replay; strongest verification is schema + payload parse',
    ),
    'capture_revisions': (
        'STRUCTURAL_ONLY',
        'capture-pipeline record — covered by canonical ingestion-run replay; strongest verification is schema + payload parse',
    ),
    'capture_revision_conflicts': (
        'STRUCTURAL_ONLY',
        'capture-pipeline record — covered by canonical ingestion-run replay; strongest verification is schema + payload parse',
    ),
    'capture_roomplan_records': (
        'STRUCTURAL_ONLY',
        'capture-pipeline record — covered by canonical ingestion-run replay; strongest verification is schema + payload parse',
    ),
    'capture_source_evidence': (
        'STRUCTURAL_ONLY',
        'capture-pipeline record — covered by canonical ingestion-run replay; strongest verification is schema + payload parse',
    ),
    'capture_raw_visual_mesh_bindings': (
        'STRUCTURAL_ONLY',
        'link/junction table — referential integrity enforced by replay of both endpoint authorities',
    ),
    'cad_acoustic_target_profiles': (
        'STRUCTURAL_ONLY',
        'acoustic target profile payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_isolation_assemblies': (
        'STRUCTURAL_ONLY',
        'isolation assembly payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_isolation_scenarios': (
        'STRUCTURAL_ONLY',
        'isolation scenario payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_isolation_estimates': (
        'STRUCTURAL_ONLY',
        'isolation estimate payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_isolation_measurements': (
        'STRUCTURAL_ONLY',
        'isolation measurement payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_rack_definitions': (
        'STRUCTURAL_ONLY',
        'rack definition payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_rack_layouts': (
        'STRUCTURAL_ONLY',
        'rack layout payload authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_project_boms': (
        'STRUCTURAL_ONLY',
        'project BOM payload authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_drawing_set_specs': (
        'STRUCTURAL_ONLY',
        'drawing-set spec payload authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_installation_drawing_sets': (
        'STRUCTURAL_ONLY',
        'installation drawing-set payload authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'cad_field_labels': (
        'STRUCTURAL_ONLY',
        'field label payload authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),
    'cad_field_label_sheets': (
        'STRUCTURAL_ONLY',
        'label sheet payload authority; canonical replay path pending — '
        'strongest verification is schema + payload parse',
    ),

}

#: Bookkeeping/editor/derived tables (the ``OPERATIONAL_METADATA`` and
#: ``EPHEMERAL`` policy classes): row counts are reported but the rows
#: carry no independent semantic claim for the audit to replay.
_NON_AUTHORITY_TABLES: dict[str, str] = {
    table: reason
    for table, (kind, reason) in _TABLE_POLICY.items()
    if kind in {'OPERATIONAL_METADATA', 'EPHEMERAL'}
}

#: Tables whose rows carry semantic payload claims but have no registered
#: canonical replay adapter; the strongest verification is schema plus
#: payload parse. Each reason documents why no replay path exists.
_STRUCTURAL_ONLY_TABLES: dict[str, str] = {
    table: reason
    for table, (kind, reason) in _TABLE_POLICY.items()
    if kind == 'STRUCTURAL_ONLY'
}


def audit_table_modes() -> dict[str, AuditCoverageMode]:
    """Every registered table mapped to its audit coverage mode.

    Completeness invariant: a persistent table absent from the returned
    mapping fails the audit with a ``coverage_gap`` diagnostic, so a new
    authority table can never silently fall back to payload parsing.
    """

    modes: dict[str, AuditCoverageMode] = {}
    for table, (kind, _reason) in _TABLE_POLICY.items():
        modes[table] = (
            'structural_only'
            if kind == 'STRUCTURAL_ONLY'
            else 'non_authority'
        )
    for table in _REPLAY_TABLES:
        modes[table] = 'replay_canonical'
    modes['capture_ingestion_runs'] = 'replay_canonical'
    for table, *_rest in _ASSET_TABLES:
        modes[table] = 'evidence_bytes'
    modes['htdt_content_blobs'] = 'evidence_bytes'
    return modes


def _classify_error(exc: BaseException) -> AuditFailureClass:
    message = str(exc).lower()
    if any(
        token in message
        for token in (
            'hash mismatch',
            'mismatch',
            'diverge',
            'non-canonical',
            'canonical derivation',
            'does not reproduce',
            're-deriv',
        )
    ):
        return 'noncanonical_derivation'
    if any(
        token in message
        for token in (
            'does not exist',
            'missing',
            'not persisted',
            'no longer resolves',
            'unresolved',
            'unknown',
            'foreign',
            'orphan',
        )
    ):
        return 'missing_evidence'
    if any(
        token in message
        for token in ('stale', 'superseded', 'outdated', 'expired')
    ):
        return 'stale_authority'
    if isinstance(exc, (json.JSONDecodeError, TypeError)):
        return 'structural'
    return 'stale_authority'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]


def _order_clause(connection: sqlite3.Connection, table: str) -> str:
    columns = _table_columns(connection, table)
    if 'seq' in columns:
        return ' ORDER BY seq'
    return ' ORDER BY rowid'


def audit_native_authority_graph(
    database_path: Path | str,
) -> AuthorityAuditReport:
    """Replay every persisted authority in ``database_path`` fail-closed.

    ``database_path`` is the SQLite file; its parent directory must carry
    the managed-asset subtree (``measurement-assets/``). Repository
    construction may initialize or migrate the schema, so callers always
    audit a clone — never a file whose bytes are pinned by a manifest.

    Each row is classified and reported rather than aborting at the first
    failure so operators see the full damage surface.
    """

    db_path = Path(database_path)
    data_dir = db_path.parent
    chain = _RepositoryChain(db_path)
    diagnostics: list[AuthorityAuditDiagnostic] = []
    checked: list[tuple[str, int]] = []
    coverage: list[tuple[str, AuditCoverageMode, int]] = []

    def record(
        authority: str,
        record_ref: str,
        exc: BaseException,
        dependency: str,
    ) -> None:
        diagnostics.append(
            AuthorityAuditDiagnostic(
                authority=authority,
                record_ref=record_ref,
                failure_class=_classify_error(exc),
                dependency=dependency,
                message=str(exc),
            )
        )

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        # ---- replay tier -------------------------------------------------
        for probe in _REPLAY_PROBES:
            if not _table_exists(connection, probe.table):
                continue
            missing_columns = [
                column
                for column in probe.columns
                if column not in _table_columns(connection, probe.table)
            ]
            if missing_columns:
                diagnostics.append(
                    AuthorityAuditDiagnostic(
                        authority=probe.authority,
                        record_ref='*',
                        failure_class='structural',
                        dependency=probe.table,
                        message=(
                            f'authority table {probe.table} lacks expected '
                            f'identity columns: {missing_columns}'
                        ),
                    )
                )
                continue
            columns = ', '.join(f'"{c}"' for c in probe.columns)
            rows = connection.execute(
                f'SELECT {columns} FROM "{probe.table}"'
                + _order_clause(connection, probe.table)
            ).fetchall()
            count = 0
            for row in rows:
                key = tuple(row[column] for column in probe.columns)
                record_ref = ':'.join(str(part) for part in key)
                try:
                    probe.verify(chain, key)
                except Exception as exc:  # noqa: BLE001 — collect, don't abort
                    record(
                        probe.authority,
                        record_ref,
                        exc,
                        dependency=probe.table,
                    )
                count += 1
            checked.append((probe.authority, count))
            coverage.append(
                (probe.authority, 'replay_canonical', count)
            )

        # ---- capture ingestion runs --------------------------------------
        if _table_exists(connection, 'capture_ingestion_runs'):
            run_columns = _table_columns(connection, 'capture_ingestion_runs')
            count = 0
            for row in connection.execute(
                'SELECT ingestion_run_id AS run_ref, plan_json '
                'FROM capture_ingestion_runs ORDER BY ingestion_run_id'
                if 'ingestion_run_id' in run_columns
                else 'SELECT lineage_digest AS run_ref, plan_json '
                'FROM capture_ingestion_runs ORDER BY lineage_digest'
            ).fetchall():
                ref = str(row['run_ref'])
                try:
                    plan = json.loads(row['plan_json'])
                    chain.repo('capture').verify_persisted_ingestion(plan)
                except Exception as exc:  # noqa: BLE001
                    record(
                        'capture_ingestion_run',
                        ref,
                        exc,
                        dependency='capture_ingestion_runs',
                    )
                count += 1
            checked.append(('capture_ingestion_run', count))
            coverage.append(
                ('capture_ingestion_run', 'replay_canonical', count)
            )

        # ---- managed-asset byte evidence ---------------------------------
        store = ManagedAssetStore(data_dir / MANAGED_ASSETS_DIRNAME)
        for table, sha_column, size_column, path_column, where in _ASSET_TABLES:
            if not _table_exists(connection, table):
                continue
            columns = _table_columns(connection, table)
            if sha_column not in columns:
                continue
            selected = [f'"{sha_column}"']
            if size_column is not None and size_column in columns:
                selected.append(f'"{size_column}"')
            if path_column is not None and path_column in columns:
                selected.append(f'"{path_column}"')
            count = 0
            sql = f'SELECT {", ".join(selected)} FROM "{table}"'
            if where is not None:
                sql += f' WHERE {where}'
            sql += _order_clause(connection, table)
            for row in connection.execute(sql).fetchall():
                digest = row[sha_column]
                ref = f'{table}:{digest}'
                try:
                    relative_path = (
                        str(row[path_column]).replace('\\', '/')
                        if path_column is not None
                        and path_column in columns
                        and row[path_column] is not None
                        else None
                    )
                    if relative_path is not None:
                        candidate = data_dir.joinpath(
                            *relative_path.split('/')
                        )
                        if (
                            '..' in relative_path.split('/')
                            or not candidate.resolve().is_relative_to(
                                data_dir.resolve()
                            )
                        ):
                            raise ValueError(
                                f'managed asset path escapes data root: '
                                f'{relative_path}'
                            )
                        path = candidate
                    else:
                        path = store.asset_path(digest)
                    if not path.exists():
                        raise ValueError(
                            f'managed asset file missing: {path.name}'
                        )
                    raw = ManagedAssetStore.read_file(path)
                    if sha256(raw).hexdigest() != digest:
                        raise ValueError(
                            f'managed asset digest mismatch for {digest}'
                        )
                    if (
                        size_column is not None
                        and size_column in columns
                        and row[size_column] is not None
                        and int(row[size_column]) != len(raw)
                    ):
                        raise ValueError(
                            f'managed asset length mismatch for {digest}'
                        )
                except Exception as exc:  # noqa: BLE001
                    record(
                        'managed_asset',
                        ref,
                        exc,
                        dependency=table,
                    )
                count += 1
            checked.append((f'managed_asset:{table}', count))
            coverage.append(
                (f'managed_asset:{table}', 'evidence_bytes', count)
            )

        # ---- retained content blobs --------------------------------------
        if _table_exists(connection, 'htdt_content_blobs'):
            count = 0
            for row in connection.execute(
                'SELECT payload_sha256, byte_count, payload_blob '
                'FROM htdt_content_blobs ORDER BY payload_sha256'
            ).fetchall():
                digest = row['payload_sha256']
                try:
                    blob = bytes(row['payload_blob'])
                    if sha256(blob).hexdigest() != digest:
                        raise ValueError(
                            f'content blob digest mismatch for {digest}'
                        )
                    if int(row['byte_count']) != len(blob):
                        raise ValueError(
                            f'content blob length mismatch for {digest}'
                        )
                except Exception as exc:  # noqa: BLE001
                    record(
                        'content_blob',
                        digest,
                        exc,
                        dependency='htdt_content_blobs',
                    )
                count += 1
            checked.append(('content_blob', count))
            coverage.append(('content_blob', 'evidence_bytes', count))

        # ---- coverage classification tier ------------------------------
        # Every persistent table is covered exactly once: a replay probe,
        # an evidence-bytes check, or an explicit _TABLE_POLICY entry.
        # Anything else is UNCLASSIFIED and fails closed.
        modes = audit_table_modes()
        tables = [
            row['name']
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        unclassified: list[str] = []
        for table in tables:
            mode = modes.get(table)
            if mode is None:
                unclassified.append(table)
                diagnostics.append(
                    AuthorityAuditDiagnostic(
                        authority=table,
                        record_ref='*',
                        failure_class='coverage_gap',
                        dependency=table,
                        message=(
                            f'persistent table {table} has no audit '
                            'coverage registration — assign it an explicit '
                            'mode in the audit coverage registry'
                        ),
                    )
                )
            elif mode == 'non_authority':
                count = connection.execute(
                    f'SELECT COUNT(*) AS n FROM "{table}"'
                ).fetchone()['n']
                checked.append((f'metadata:{table}', count))
                coverage.append(
                    (f'non_authority:{table}', 'non_authority', count)
                )

        # ---- structural payload tier -------------------------------------
        for table in sorted(_STRUCTURAL_ONLY_TABLES):
            if not _table_exists(connection, table):
                continue
            payload_columns = [
                column
                for column in _table_columns(connection, table)
                if column.endswith('_json')
            ]
            count = 0
            if payload_columns:
                select = ', '.join(f'"{c}"' for c in payload_columns)
                for row in connection.execute(
                    f'SELECT {select} FROM "{table}"'
                    + _order_clause(connection, table)
                ).fetchall():
                    for column in payload_columns:
                        raw = row[column]
                        if raw is None:
                            continue
                        try:
                            json.loads(raw)
                        except Exception as exc:  # noqa: BLE001
                            record(
                                table,
                                f'row {count}',
                                exc,
                                dependency=f'{table}.{column}',
                            )
                    count += 1
            checked.append((f'structural:{table}', count))
            coverage.append(
                (f'structural:{table}', 'structural_only', count)
            )
    finally:
        connection.close()

    return AuthorityAuditReport(
        database_path=db_path,
        checked=tuple(checked),
        diagnostics=tuple(diagnostics),
        unclassified_tables=tuple(unclassified),
        coverage=tuple(coverage),
    )


def assert_native_authority_graph(
    database_path: Path | str,
) -> AuthorityAuditReport:
    """Audit ``database_path`` and raise :class:`AuthorityAuditError`."""

    report = audit_native_authority_graph(database_path)
    if not report.ok:
        raise AuthorityAuditError(report)
    return report
