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

    def summary(self) -> str:
        if self.ok:
            total = sum(count for _, count in self.checked)
            counts = self.coverage_counts
            return (
                f'authority graph audit passed ({total} records checked; '
                f"{counts['replayed_records']} replayed, "
                f"{counts['structural_only_tables']} structural-only tables, "
                f"{counts['operational_metadata_tables']} operational "
                'metadata tables, 0 unclassified)'
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
    # ---- #718 hardening families ---------------------------------------
    _ReplayProbe(
        'design_comparison_set',
        'cad_design_comparison_sets',
        ('set_id',),
        _get('comparison', 'verify_persisted_set'),
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
        ('applied_id',),
        _get('presets', 'verify_persisted_applied_state'),
    ),
    _ReplayProbe(
        'preset_measurement_binding',
        'cad_preset_measurement_bindings',
        ('binding_id',),
        _get('presets', 'verify_persisted_measurement_binding'),
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
        ('plan_id',),
        _get('health', 'verify_persisted_plan'),
    ),
    _ReplayProbe(
        'health_check_run',
        'cad_health_check_runs',
        ('run_id',),
        _get('health', 'verify_persisted_run'),
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
        ('restore_id',),
        _get('checkpoints', 'verify_persisted_restore'),
    ),
    _ReplayProbe(
        'project_registry',
        'project_registry',
        ('project_id',),
        _get('project_library', 'verify_project_registration'),
    ),
    _ReplayProbe(
        'project_tombstone',
        'project_tombstones',
        ('tombstone_id',),
        _get('project_library', 'verify_persisted_tombstone'),
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

# Tables whose rows carry a JSON payload but have no repository replay path
# (external-resolver domains and link/metadata tables). They are still
# enumerated so raw payload corruption cannot slip through the audit.
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
    'scene_document_heads': (
        'OPERATIONAL_METADATA',
        'mutable head pointer derived from scene_revisions',
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
    'authoring_constraint_sets': (
        'OPERATIONAL_METADATA',
        'UI-authored constraint hint payloads — convenience, not design '
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
    'cad_equipment_binding_semantics': (
        'STRUCTURAL_ONLY',
        'binding-semantics payload authority; canonical replay path '
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
    'cad_measurement_corrections': (
        'STRUCTURAL_ONLY',
        'measurement correction payload authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'cad_measurement_dispositions': (
        'STRUCTURAL_ONLY',
        'measurement disposition payload authority; canonical replay '
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
    'cad_measurement_runner_events': (
        'OPERATIONAL_METADATA',
        'measurement runner event log — operational bookkeeping',
    ),
    'cad_measurement_runner_plans': (
        'OPERATIONAL_METADATA',
        'measurement runner plan bookkeeping — derived operational state',
    ),
    'cad_measurement_runner_runs': (
        'OPERATIONAL_METADATA',
        'measurement runner run bookkeeping — derived operational state',
    ),
    'cad_dependency_resolution_events': (
        'OPERATIONAL_METADATA',
        'append-only dependency resolution log — operational bookkeeping',
    ),
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
    'cad_analysis_studies': (
        'STRUCTURAL_ONLY',
        'analysis study authority; canonical replay path pending — '
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
    'cad_design_briefs': (
        'STRUCTURAL_ONLY',
        'design brief authority; canonical replay path pending — '
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
    'cad_external_dependencies': (
        'STRUCTURAL_ONLY',
        'external dependency authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
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
    'cad_installed_definition_bindings': (
        'STRUCTURAL_ONLY',
        'installed definition binding authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_installed_device_observations': (
        'STRUCTURAL_ONLY',
        'installed device observation authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_installed_equipment_instances': (
        'STRUCTURAL_ONLY',
        'installed equipment instance authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_installed_equipment_replacements': (
        'STRUCTURAL_ONLY',
        'installed equipment replacement authority; canonical replay '
        'path pending — strongest verification is schema + payload '
        'parse',
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
}


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

        # ---- coverage classification tier ------------------------------
        # Every persistent table is covered exactly once: a replay probe,
        # an evidence-bytes check, or an explicit _TABLE_POLICY entry.
        # Anything else is UNCLASSIFIED and fails closed.
        asset_tables = {table for table, *_ in _ASSET_TABLES}
        covered = (
            _REPLAY_TABLES
            | asset_tables
            | {'capture_ingestion_runs', 'htdt_content_blobs'}
        )
        tables = [
            row['name']
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        unclassified: list[str] = []
        for table in tables:
            if table in covered:
                continue
            policy = _TABLE_POLICY.get(table)
            if policy is None:
                unclassified.append(table)
                diagnostics.append(
                    AuthorityAuditDiagnostic(
                        authority='coverage',
                        record_ref=table,
                        failure_class='unclassified',
                        dependency=table,
                        message=(
                            f'table {table} has no coverage policy entry — '
                            'classify it (replay probe, evidence bytes, '
                            'STRUCTURAL_ONLY, EPHEMERAL, or '
                            'OPERATIONAL_METADATA) before the audit can '
                            'claim coverage'
                        ),
                    )
                )
                continue
            policy_kind, _reason = policy
            label = (
                'metadata'
                if policy_kind in {'OPERATIONAL_METADATA', 'EPHEMERAL'}
                else 'structural'
            )
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
            checked.append((f'{label}:{table}', count))
    finally:
        connection.close()

    return AuthorityAuditReport(
        database_path=db_path,
        checked=tuple(checked),
        diagnostics=tuple(diagnostics),
        unclassified_tables=tuple(unclassified),
    )


def assert_native_authority_graph(
    database_path: Path | str,
) -> AuthorityAuditReport:
    """Audit ``database_path`` and raise :class:`AuthorityAuditError`."""

    report = audit_native_authority_graph(database_path)
    if not report.ok:
        raise AuthorityAuditError(report)
    return report
