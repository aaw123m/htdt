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
    coverage: tuple[tuple[str, AuditCoverageMode, int], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.diagnostics

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
            return (
                f'authority graph audit passed ({total} records replayed)'
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
        if name == 'presets':
            from .cad_operating_preset_repository import (
                CadOperatingPresetRepository,
            )

            return CadOperatingPresetRepository(scene)
        if name == 'checkpoints':
            from .cad_design_checkpoint_repository import (
                CadDesignCheckpointRepository,
            )

            return CadDesignCheckpointRepository(scene)
        if name == 'health':
            from .cad_system_health_repository import (
                CadSystemHealthRepository,
            )

            return CadSystemHealthRepository(scene)
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

            return CadInstalledEquipmentRepository(scene)
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
    plans = chain.repo('measurement').list_measurement_plans(search_spec_id)
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
    lineage = chain.repo('quality').list_lineage(document_id)
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
    samples = chain.repo('robustness').list_samples(robustness_spec_id)
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
    evaluations = chain.repo('robustness').list_evaluations(robustness_spec_id)
    if evaluation_id not in {
        evaluation.evaluation_id for evaluation in evaluations
    }:
        raise ValueError(
            f'robustness evaluation {evaluation_id} no longer resolves for '
            f'RobustnessSpec {robustness_spec_id}'
        )
    return evaluations


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
    presets = chain.repo('presets')
    applied = _require(
        presets.get_applied_state(applied_id),
        f'applied preset state {applied_id}',
    )
    if applied.preset_id != preset_id:
        raise ValueError(
            f'applied preset state {applied_id} binds preset '
            f'{applied.preset_id}, not recorded {preset_id}'
        )
    preset = _require(
        presets.get_preset(preset_id), f'operating preset {preset_id}'
    )
    if preset.preset_sha256 != applied.preset_sha256:
        raise ValueError(
            f'applied preset state {applied_id} pins preset_sha256 '
            f'{applied.preset_sha256} but the recorded preset resolves to '
            f'{preset.preset_sha256}'
        )
    return applied


def _verify_preset_binding(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    binding_id, preset_id = key
    presets = chain.repo('presets')
    binding = _require(
        presets.get_measurement_binding(binding_id),
        f'preset measurement binding {binding_id}',
    )
    if binding.preset_id != preset_id:
        raise ValueError(
            f'preset binding {binding_id} binds preset {binding.preset_id}, '
            f'not recorded {preset_id}'
        )
    _require(presets.get_preset(preset_id), f'operating preset {preset_id}')
    measurements = chain.repo('measurement')
    for measurement_id in binding.measurement_ids:
        _require(
            measurements.get_measurement(measurement_id),
            f'measurement {measurement_id} bound to preset {preset_id}',
        )
    return binding


def _verify_checkpoint_restore(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    restore_id, checkpoint_id = key
    checkpoints = chain.repo('checkpoints')
    restore = _require(
        checkpoints.get_restore(restore_id),
        f'checkpoint restore {restore_id}',
    )
    if restore.checkpoint_id != checkpoint_id:
        raise ValueError(
            f'checkpoint restore {restore_id} binds checkpoint '
            f'{restore.checkpoint_id}, not recorded {checkpoint_id}'
        )
    _require(
        checkpoints.get_checkpoint(checkpoint_id),
        f'design checkpoint {checkpoint_id}',
    )
    return restore


def _verify_health_plan(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    plan_id, baseline_id = key
    health = chain.repo('health')
    plan = _require(health.get_plan(plan_id), f'health check plan {plan_id}')
    if plan.baseline_id != baseline_id:
        raise ValueError(
            f'health check plan {plan_id} binds baseline '
            f'{plan.baseline_id}, not recorded {baseline_id}'
        )
    _require(
        health.get_baseline(baseline_id), f'health baseline {baseline_id}'
    )
    return plan


def _verify_health_run(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    run_id, plan_id = key
    health = chain.repo('health')
    run = _require(health.get_run(run_id), f'health check run {run_id}')
    if run.plan_id != plan_id:
        raise ValueError(
            f'health check run {run_id} binds plan {run.plan_id}, '
            f'not recorded {plan_id}'
        )
    _require(health.get_plan(plan_id), f'health check plan {plan_id}')
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
    sets = chain.repo('design_comparisons').list_sets(document_id)
    record = _require(
        next((s for s in sets if s.set_id == set_id), None),
        f'design comparison set {set_id} in document {document_id}',
    )
    return _verify_supersedes_chain(
        record, sets, 'set_id', 'supersedes_set_id', 'comparison set'
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


def _verify_project_tombstone(
    chain: _RepositoryChain, key: tuple[Any, ...]
) -> Any:
    tombstone_id, project_id = key
    tombstones = chain.repo('projects').list_tombstones()
    record = _require(
        next(
            (t for t in tombstones if t.tombstone_id == tombstone_id),
            None,
        ),
        f'project tombstone {tombstone_id}',
    )
    if record.project_id != project_id:
        raise ValueError(
            f'project tombstone {tombstone_id} binds project '
            f'{record.project_id}, not recorded {project_id}'
        )
    return record


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
        _get('roomsim', 'get_batch_spec'),
    ),
    _ReplayProbe(
        'roomsim_attempt',
        'cad_roomsim_candidate_attempts',
        ('attempt_id',),
        _get('roomsim', 'get_attempt'),
    ),
    _ReplayProbe(
        'objective_evaluation',
        'cad_objective_evaluations',
        ('evaluation_id',),
        _get('objective', 'get_evaluation'),
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
    _ReplayProbe(
        'operating_preset',
        'cad_operating_presets',
        ('preset_id',),
        _get('presets', 'get_preset'),
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
        'design_checkpoint',
        'cad_design_checkpoints',
        ('checkpoint_id',),
        _get('checkpoints', 'get_checkpoint'),
    ),
    _ReplayProbe(
        'checkpoint_restore',
        'cad_checkpoint_restores',
        ('restore_id', 'checkpoint_id'),
        _verify_checkpoint_restore,
    ),
    _ReplayProbe(
        'constraint_snapshot',
        'cad_constraint_snapshots',
        ('snapshot_id',),
        _get('checkpoints', 'get_snapshot'),
    ),
    _ReplayProbe(
        'health_baseline',
        'cad_health_baselines',
        ('baseline_id',),
        _get('health', 'get_baseline'),
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
        'design_comparison_set',
        'cad_design_comparison_sets',
        ('set_id', 'document_id'),
        _verify_comparison_set,
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
        'project_registry_entry',
        'project_registry',
        ('project_id',),
        _get('projects', 'get_project'),
    ),
    _ReplayProbe(
        'project_tombstone',
        'project_tombstones',
        ('tombstone_id', 'project_id'),
        _verify_project_tombstone,
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

# Audit coverage registry: every persisted table must be explicitly assigned
# one coverage mode. A table absent from every registry below fails the audit
# with a ``coverage_gap`` diagnostic — a new authority table can never
# silently degrade to the structural fallback.
#
# * REPLAY_CANONICAL — ``_REPLAY_PROBES`` plus the capture-ingestion verifier.
# * EVIDENCE_BYTES — ``_ASSET_TABLES`` plus the ``htdt_content_blobs`` store.
# * STRUCTURAL_ONLY_WITH_RATIONALE — ``_STRUCTURAL_ONLY_TABLES`` below, each
#   entry documenting why no canonical replay adapter exists.
# * TRANSIENT_OR_NON_AUTHORITY — ``_NON_AUTHORITY_TABLES`` below; the rows are
#   operational/ephemeral state that downstream authorities never consume.
_NON_AUTHORITY_TABLES: dict[str, str] = {
    'sqlite_sequence': 'sqlite rowid bookkeeping, not a persisted authority',
    'native_schema_metadata': 'schema bookkeeping, not a persisted authority',
    'native_schema_migrations': 'schema bookkeeping, not a persisted authority',
    'editor_view_states': (
        'disposable editor state — never consumed by authorities'
    ),
    'editor_camera_states': (
        'disposable editor state — never consumed by authorities'
    ),
    'editor_named_views': (
        'disposable editor state — never consumed by authorities'
    ),
    'scene_recovery_snapshots': (
        'recovery cache re-derivable from scene_revisions'
    ),
    'floor_plan_underlays': (
        'authoring aid — underlay bytes verified via the content-blob tier'
    ),
    'scene_revision_labels': 'display labels — non-normative annotation',
    'seating_layout_specs': (
        'editor convenience spec — layout authority is the scene graph'
    ),
    'capture_inbox_items': 'transient intake queue, not retained authority',
    'capture_inbox_promotions': (
        'transient intake queue, not retained authority'
    ),
    'capture_inbox_registrations': (
        'transient intake queue, not retained authority'
    ),
    'capture_inbox_supersessions': (
        'transient intake queue, not retained authority'
    ),
    'capture_receiver_config': (
        'device pairing operational state, non-normative'
    ),
    'capture_receiver_deliveries': (
        'device pairing operational state, non-normative'
    ),
    'capture_receiver_pairings': (
        'device pairing operational state, non-normative'
    ),
    'capture_mission_packages': 'device mission staging, non-normative',
    'field_return_contributions': (
        'transient staging queue, not retained authority'
    ),
    'cad_r140_execution_cache': (
        'execution cache — derived runtime artifact'
    ),
    'cad_r140_execution_schedules': (
        'execution runtime state — derived artifact'
    ),
    'cad_r140_execution_attempts': (
        'execution runtime state — derived artifact'
    ),
    'cad_r140_execution_tasks': 'execution runtime state — derived artifact',
    'project_action_items': (
        'project-management annotation, non-normative'
    ),
    'ci_marker': (
        'CI-injected backup/restore round-trip marker — never a product '
        'authority row'
    ),
}

_JUNCTION_RATIONALE = (
    'link/junction table — referential integrity enforced by replay of '
    'both endpoint authorities'
)
_LEGACY_RATIONALE = (
    'legacy generic store — superseded by cad_* domain authorities'
)
_CAPTURE_RATIONALE = (
    'capture-pipeline record — covered by canonical ingestion-run replay'
)
_NO_ADAPTER_RATIONALE = (
    'structural payload integrity — no dedicated canonical replay adapter '
    'registered for this family'
)

# Tables whose rows carry a JSON payload but no registered canonical replay
# adapter (external-resolver domains, link/legacy tables, derived solver and
# pipeline artifacts). They are still enumerated so raw payload corruption
# cannot slip through the audit.
_STRUCTURAL_ONLY_TABLES: dict[str, str] = {
    table: rationale
    for tables, rationale in (
        (
            (
                'asset_links',
                'cad_acoustic_solver_dispatch_bindings',
                'cad_evidence_subjects',
                'cad_field_evidence_targets',
                'cad_hybrid_prediction_provider_bindings',
                'cad_hybrid_prediction_provider_objectives',
                'cad_prediction_provider_bindings',
                'cad_prediction_provider_objectives',
                'cad_materialized_pattern_points',
                'cad_measurement_attachments',
                'cad_measurement_target_patterns',
                'cad_plan_target_bindings',
                'cad_screen_transfer_selections',
                'cad_source_response_selections',
                'cad_listener_pose_selections',
                'cad_surface_material_assignments',
                'cad_topology_space_options',
                'cad_r120_compile_inputs',
                'cad_r120_leak_diagnostic_inputs',
                'cad_deterministic_ga_execution_inputs',
                'cad_deterministic_path_artifacts',
                'cad_intervention_alternatives',
                'capture_ingestion_authority_links',
                'capture_ingestion_mesh_links',
                'capture_ingestion_source_links',
                'capture_raw_visual_mesh_bindings',
            ),
            _JUNCTION_RATIONALE,
        ),
        (
            (
                'assets',
                'comparisons',
                'constraint_sets',
                'contexts',
                'datasets',
                'measurements',
                'metadata',
                'projects',
                'search_specs',
                'sessions',
            ),
            _LEGACY_RATIONALE,
        ),
        (
            (
                'capture_authority_records',
                'capture_bundles',
                'capture_connected_space_documents',
                'capture_coordinate_authorities',
                'capture_ingestion_lineages',
                'capture_mesh_compositions',
                'capture_revisions',
                'capture_revision_conflicts',
                'capture_roomplan_records',
                'capture_semantic_promotions',
                'capture_source_evidence',
                'physical_space_models',
            ),
            _CAPTURE_RATIONALE,
        ),
        (
            (
                'cad_acoustic_materials',
                'cad_acoustic_solver_adapters',
                'cad_acoustic_solver_results',
                'cad_acoustic_treatment_comparisons',
                'cad_ambient_comparisons',
                'cad_ambient_conditions',
                'cad_ambient_criteria',
                'cad_ambient_evaluations',
                'cad_ambient_profiles',
                'cad_amplifier_electrical_limits',
                'cad_applied_settings',
                'cad_av_latency_measurements',
                'cad_av_sync_conditions',
                'cad_cable_runs',
                'cad_calibration_lifecycle_events',
                'cad_commissioning_plans',
                'cad_commissioning_runs',
                'cad_cost_evaluations',
                'cad_cost_records',
                'cad_current_topologies',
                'cad_direct_view_evaluations',
                'cad_direct_view_specifications',
                'cad_environment_profiles',
                'cad_environment_selections',
                'cad_evidence_observations',
                'cad_field_evidence',
                'cad_frequency_resolved_evaluations',
                'cad_gain_structure_evaluations',
                'cad_gain_structure_scenarios',
                'cad_hybrid_acoustic_results',
                'cad_hybrid_prediction_providers',
                'cad_hybrid_stitching_policies',
                'cad_installation_contexts',
                'cad_installation_datums',
                'cad_intervention_study_specs',
                'cad_ir_analysis_results',
                'cad_ir_analysis_specs',
                'cad_layout_profiles',
                'cad_line_level_stages',
                'cad_listener_poses',
                'cad_multi_seat_results',
                'cad_multi_seat_sets',
                'cad_multifidelity_finalizations',
                'cad_multifidelity_plans',
                'cad_multifidelity_screening_evaluations',
                'cad_multifidelity_stage_results',
                'cad_o90_robust_pareto_evaluations',
                'cad_prediction_providers',
                'cad_project_notes',
                'cad_proposal_objective_result_authorities',
                'cad_proposal_perturbation_samples',
                'cad_proposal_robust_pareto_evaluations',
                'cad_proposal_robustness_evaluations',
                'cad_proposal_robustness_specs',
                'cad_r140_execution_results',
                'cad_r140_gpu_authorities',
                'cad_r140_resource_estimates',
                'cad_raw_mesh_repair_bundles',
                'cad_reconciliation_decisions',
                'cad_room_operating_states',
                'cad_screen_transfers',
                'cad_seat_priority_profiles',
                'cad_source_responses',
                'cad_speaker_impedances',
                'cad_target_curve_profiles',
                'cad_tolerance_profiles',
                'cad_topology_candidate_variants',
                'cad_topology_placement_candidates',
                'cad_topology_search_specs',
                'cad_topology_spaces',
                'cad_video_geometry_workspaces',
                'project_templates',
                'r150_path_frequency_response_artifacts',
                'r160_numerical_hybrid_responses',
                'template_instantiations',
            ),
            _NO_ADAPTER_RATIONALE,
        ),
    )
    for table in tables
}


def audit_table_modes() -> dict[str, AuditCoverageMode]:
    """Every registered table mapped to its audit coverage mode.

    Completeness invariant: a persistent table absent from the returned
    mapping fails the audit with a ``coverage_gap`` diagnostic, so a new
    authority table can never silently fall back to payload parsing.
    """

    modes: dict[str, AuditCoverageMode] = {}
    for table in _REPLAY_TABLES:
        modes[table] = 'replay_canonical'
    modes['capture_ingestion_runs'] = 'replay_canonical'
    for table, *_rest in _ASSET_TABLES:
        modes[table] = 'evidence_bytes'
    modes['htdt_content_blobs'] = 'evidence_bytes'
    for table in _STRUCTURAL_ONLY_TABLES:
        modes[table] = 'structural_only'
    for table in _NON_AUTHORITY_TABLES:
        modes[table] = 'non_authority'
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

        # ---- coverage registry -------------------------------------------
        # Every persisted table must hold an explicit registry entry; an
        # unregistered table fails the audit instead of silently degrading
        # to a payload-parse fallback.
        modes = audit_table_modes()
        tables = [
            row['name']
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            mode = modes.get(table)
            if mode is None:
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
            if not payload_columns:
                continue
            count = 0
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
