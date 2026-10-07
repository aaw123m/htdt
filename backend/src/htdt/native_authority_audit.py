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

from .cad_schema import connect_sqlite
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

    def close(self) -> None:
        """Release file handles held by the built repositories.

        The audit runs on a throwaway clone that callers unlink right after
        return; a repository whose read path keeps a pooled connection
        would hold the file open past the clone's lifetime on Windows.
        """
        for repo in self._repos.values():
            close = getattr(repo, 'close', None)
            if callable(close):
                close()
        self._repos.clear()

    def _build(self, name: str) -> Any:  # noqa: C901
        if name == 'scene':
            from .cad_repository import SceneRepository

            return SceneRepository(self.db_path)
        scene = self.repo('scene')
        if name == 'acceptance':
            from .cad_acceptance_repository import AcceptanceRunRepository

            return AcceptanceRunRepository(self.db_path)
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
        if name == 'prediction_measurement_registration':
            from .cad_prediction_measurement_registration_repository import (
                CadPredictionMeasurementRegistrationRepository,
            )

            return CadPredictionMeasurementRegistrationRepository(scene)
        if name == 'correction_qualification':
            from .cad_correction_qualification_repository import (
                CadCorrectionQualificationRepository,
            )

            return CadCorrectionQualificationRepository(scene)
        if name == 'measurement_evidence':
            from .cad_measurement_evidence_repository import (
                CadMeasurementEvidenceRepository,
            )

            return CadMeasurementEvidenceRepository(scene)
        if name == 'decision_rule':
            from .cad_decision_rule_repository import (
                CadDecisionRuleRepository,
            )

            return CadDecisionRuleRepository(scene)
        if name == 'robust_design':
            from .cad_robust_design_repository import (
                CadRobustDesignRepository,
            )

            return CadRobustDesignRepository(scene)
        if name == 'stimulus_registry':
            from .cad_stimulus_registry_repository import (
                CadStimulusRegistryRepository,
            )

            return CadStimulusRegistryRepository(scene)
        if name == 'bass_qualification':
            from .cad_bass_qualification_repository import (
                CadBassQualificationRepository,
            )

            return CadBassQualificationRepository(scene)
        if name == 'external_standards':
            from .cad_external_standards_repository import (
                CadExternalStandardsRepository,
            )

            return CadExternalStandardsRepository(scene)
        if name == 'device_snapshot':
            from .cad_device_snapshot_repository import (
                CadDeviceSnapshotRepository,
            )

            return CadDeviceSnapshotRepository(scene)
        if name == 'spatial_campaign':
            from .cad_spatial_campaign_repository import (
                CadSpatialCampaignRepository,
            )

            return CadSpatialCampaignRepository(scene)
        if name == 'rp32_commissioning':
            from .cad_rp32_repository import (
                CadRp32Repository,
            )

            return CadRp32Repository(scene)
        if name == 'rp22_profile':
            from .cad_rp22_profile_repository import (
                CadRP22ProfileRepository,
            )

            return CadRP22ProfileRepository(scene)
        if name == 'response_target':
            from .cad_response_target_repository import (
                CadResponseTargetRepository,
            )

            return CadResponseTargetRepository(scene)
        if name == 'room_noise_metric':
            from .cad_room_noise_metrics_repository import (
                CadRoomNoiseMetricRepository,
            )

            return CadRoomNoiseMetricRepository(scene)
        if name == 'sti':
            from .cad_sti_repository import (
                CadSTIRepository,
            )

            return CadSTIRepository(scene)
        if name == 'loudness':
            from .cad_loudness_repository import (
                CadLoudnessRepository,
            )

            return CadLoudnessRepository(scene)
        if name == 'multi_sub_optimization':
            from .cad_multi_sub_optimization_repository import (
                CadMultiSubOptimizationRepository,
            )

            return CadMultiSubOptimizationRepository(scene)
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
        if name == 'presentation':
            from .cad_presentation_repository import (
                CadPresentationRepository,
            )

            return CadPresentationRepository(scene)
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
        if name == 'electrical_compatibility':
            from .cad_electrical_compatibility_repository import (
                CadElectricalCompatibilityRepository,
            )

            return CadElectricalCompatibilityRepository(scene)
        if name == 'wiring_trace':
            from .cad_wiring_trace_repository import (
                CadWiringTraceRepository,
            )

            return CadWiringTraceRepository(scene)
        if name == 'health_drift':
            from .cad_health_drift_repository import (
                CadHealthDriftRepository,
            )

            return CadHealthDriftRepository(scene)
        if name == 'substitution_impact':
            from .cad_substitution_impact_repository import (
                CadSubstitutionImpactRepository,
            )

            return CadSubstitutionImpactRepository(scene)
        if name == 'av_latency':
            from .cad_av_latency_repository import (
                CadAVLatencyRepository,
            )

            return CadAVLatencyRepository(scene)
        if name == 'hdmi_verification':
            from .cad_hdmi_verification_repository import (
                CadHDMIVerificationRepository,
            )

            return CadHDMIVerificationRepository(scene)
        if name == 'network_av':
            from .cad_network_av_repository import (
                CadNetworkAVRepository,
            )

            return CadNetworkAVRepository(scene)
        if name == 'isolation_authority':
            from .cad_isolation_authority_repository import (
                CadIsolationAuthorityRepository,
            )

            return CadIsolationAuthorityRepository(scene)
        if name == 'mechanical_noise':
            from .cad_mechanical_noise_repository import (
                CadMechanicalNoiseRepository,
            )

            return CadMechanicalNoiseRepository(scene)
        if name == 'seating_acoustics':
            from .cad_seating_acoustics_repository import (
                CadSeatingAcousticsRepository,
            )

            return CadSeatingAcousticsRepository(scene)
        if name == 'ifc_interop':
            from .cad_ifc_repository import CadIfcInteropRepository

            return CadIfcInteropRepository(scene)
        if name == 'performance_facts':
            from .cad_performance_facts_repository import (
                CadPerformanceFactsRepository,
            )

            return CadPerformanceFactsRepository(scene)
        # REV56-OPS probes already reference these authorities; the
        # factory branches were missing, so a populated table raised
        # KeyError at replay time.
        if name == 'security_authority':
            from .cad_security_authority_repository import (
                CadSecurityAuthorityRepository,
            )

            return CadSecurityAuthorityRepository(scene)
        if name == 'control_scenario':
            from .cad_control_scenario_repository import (
                CadControlScenarioRepository,
            )

            return CadControlScenarioRepository(scene)
        if name == 'safe_listening':
            from .cad_safe_listening_repository import (
                CadSafeListeningRepository,
            )

            return CadSafeListeningRepository(scene)
        if name == 'infrastructure':
            from .cad_infrastructure_repository import (
                CadInfrastructureRepository,
            )

            return CadInfrastructureRepository(scene)
        if name == 'render_path':
            from .cad_render_path_repository import (
                CadRenderPathRepository,
            )

            return CadRenderPathRepository(scene)
        if name == 'electrical_noise':
            from .cad_electrical_noise_repository import (
                CadElectricalNoiseRepository,
            )

            return CadElectricalNoiseRepository(scene)
        # REV57-METRO authorities.
        if name == 'timebase_authority':
            from .cad_timebase_authority_repository import (
                CadTimebaseAuthorityRepository,
            )

            return CadTimebaseAuthorityRepository(scene)
        if name == 'evidence_bundle':
            from .cad_evidence_bundle_repository import (
                CadEvidenceBundleRepository,
            )

            return CadEvidenceBundleRepository(scene)
        if name == 'calibration_lifecycle':
            from .cad_calibration_lifecycle_repository import (
                CadCalibrationLifecycleRepository,
            )

            return CadCalibrationLifecycleRepository(scene)
        # REV57-PHYS authorities.
        if name == 'geometry_survey':
            from .cad_geometry_survey_repository import (
                CadGeometrySurveyRepository,
            )

            return CadGeometrySurveyRepository(scene)
        if name == 'installed_source':
            from .cad_installed_source_boundary_repository import (
                CadInstalledSourceBoundaryRepository,
            )

            return CadInstalledSourceBoundaryRepository(scene)
        if name == 'porous_absorber':
            from .cad_porous_absorber_repository import (
                CadPorousAbsorberRepository,
            )

            return CadPorousAbsorberRepository(scene)
        # REV57-PROJ authorities.
        if name == 'spatial_image':
            from .cad_spatial_image_repository import (
                CadSpatialImageRepository,
            )

            return CadSpatialImageRepository(scene)
        if name == 'projection_geometry':
            from .cad_projection_geometry_repository import (
                CadProjectionGeometryRepository,
            )

            return CadProjectionGeometryRepository(scene)
        if name == 'hushbox':
            from .cad_hushbox_repository import (
                CadHushboxRepository,
            )

            return CadHushboxRepository(scene)
        if name == 'optical_safety':
            from .cad_optical_safety_repository import (
                CadOpticalSafetyRepository,
            )

            return CadOpticalSafetyRepository(scene)
        # REV57-DISP authorities.
        if name == 'direct_view_display':
            from .cad_direct_view_display_repository import (
                CadDirectViewDisplayRepository,
            )

            return CadDirectViewDisplayRepository(scene)
        if name == 'observer_metamerism':
            from .cad_observer_metamerism_repository import (
                CadObserverMetamerismRepository,
            )

            return CadObserverMetamerismRepository(scene)
        if name == 'viewing_environment':
            from .cad_viewing_environment_repository import (
                CadViewingEnvironmentRepository,
            )

            return CadViewingEnvironmentRepository(scene)
        # REV57-AUD authorities.
        if name == 'channel_identity':
            from .cad_channel_identity_repository import (
                CadChannelIdentityRepository,
            )

            return CadChannelIdentityRepository(scene)
        if name == 'coverage_aim':
            from .cad_coverage_aim_repository import (
                CadCoverageAimRepository,
            )

            return CadCoverageAimRepository(scene)
        if name == 'instance_variation':
            from .cad_instance_variation_repository import (
                CadInstanceVariationRepository,
            )

            return CadInstanceVariationRepository(scene)
        if name == 'media_playback':
            from .cad_media_playback_repository import (
                CadMediaPlaybackRepository,
            )

            return CadMediaPlaybackRepository(scene)
        # REV57-INST authorities.
        if name == 'hvac':
            from .cad_hvac_repository import (
                CadHvacRepository,
            )

            return CadHvacRepository(scene)
        if name == 'playback_reference':
            from .cad_playback_reference_repository import (
                CadPlaybackReferenceRepository,
            )

            return CadPlaybackReferenceRepository(scene)
        if name == 'treatment_asbuilt':
            from .cad_treatment_asbuilt_repository import (
                CadTreatmentAsBuiltRepository,
            )

            return CadTreatmentAsBuiltRepository(scene)
        if name == 'tactile_vibration':
            from .cad_tactile_vibration_repository import (
                CadTactileVibrationRepository,
            )

            return CadTactileVibrationRepository(scene)
        # REV57-MOUNT authority.
        if name == 'mounting_support':
            from .cad_mounting_support_repository import (
                CadMountingSupportRepository,
            )

            return CadMountingSupportRepository(scene)
        # REV58-MEASCHAIN authorities.
        if name == 'measchain_linearity':
            from .cad_measchain_linearity_repository import (
                CadMeasChainLinearityRepository,
            )

            return CadMeasChainLinearityRepository(scene)
        if name == 'sweep_deconvolution':
            from .cad_sweep_deconvolution_repository import (
                CadSweepDeconvolutionRepository,
            )

            return CadSweepDeconvolutionRepository(scene)
        if name == 'excitation_source':
            from .cad_excitation_source_repository import (
                CadExcitationSourceRepository,
            )

            return CadExcitationSourceRepository(scene)
        # REV58-DSPDECAY authorities.
        if name == 'dsp_realization':
            from .cad_dsp_realization_repository import (
                CadDspRealizationRepository,
            )

            return CadDspRealizationRepository(scene)
        if name == 'decay_processing':
            from .cad_decay_processing_repository import (
                CadDecayProcessingRepository,
            )

            return CadDecayProcessingRepository(scene)
        if name == 'boundary_realizability':
            from .cad_boundary_realizability_repository import (
                CadBoundaryRealizabilityRepository,
            )

            return CadBoundaryRealizabilityRepository(scene)
        # REV58-NUMERIC authorities.
        if name == 'wave_fidelity':
            from .cad_wave_fidelity_repository import (
                CadWaveFidelityRepository,
            )

            return CadWaveFidelityRepository(scene)
        if name == 'geometric_fidelity':
            from .cad_geometric_fidelity_repository import (
                CadGeometricFidelityRepository,
            )

            return CadGeometricFidelityRepository(scene)
        if name == 'hybrid_handoff':
            from .cad_hybrid_handoff_repository import (
                CadHybridHandoffRepository,
            )

            return CadHybridHandoffRepository(scene)
        # REV58-AUDIOMODEL authorities.
        if name == 'source_origin':
            from .cad_source_origin_repository import (
                CadSourceOriginRepository,
            )

            return CadSourceOriginRepository(scene)
        if name == 'source_field_applicability':
            from .cad_source_field_applicability_repository import (
                CadSourceFieldApplicabilityRepository,
            )

            return CadSourceFieldApplicabilityRepository(scene)
        if name == 'directivity_resolution':
            from .cad_directivity_resolution_repository import (
                CadDirectivityResolutionRepository,
            )

            return CadDirectivityResolutionRepository(scene)
        if name == 'source_coherence':
            from .cad_source_coherence_repository import (
                CadSourceCoherenceRepository,
            )

            return CadSourceCoherenceRepository(scene)
        if name == 'scattering_model':
            from .cad_scattering_model_repository import (
                CadScatteringModelRepository,
            )

            return CadScatteringModelRepository(scene)
        if name == 'edge_diffraction':
            from .cad_edge_diffraction_repository import (
                CadEdgeDiffractionRepository,
            )

            return CadEdgeDiffractionRepository(scene)
        # REV58-IDENT authorities.
        if name == 'logarithmic_quantity':
            from .cad_logarithmic_quantity_repository import (
                CadLogQuantityRepository,
            )

            return CadLogQuantityRepository(scene)
        if name == 'parameter_identifiability':
            from .cad_parameter_identifiability_repository import (
                CadParameterIdentifiabilityRepository,
            )

            return CadParameterIdentifiabilityRepository(scene)
        if name == 'validation_statistics':
            from .cad_validation_statistics_repository import (
                CadValidationStatisticsRepository,
            )

            return CadValidationStatisticsRepository(scene)
        # REV58-VALIDMETH authorities.
        if name == 'optimizer_qualification':
            from .cad_optimizer_qualification_repository import (
                CadOptimizerQualificationRepository,
            )

            return CadOptimizerQualificationRepository(scene)
        if name == 'eigenmode_validation':
            from .cad_eigenmode_validation_repository import (
                CadEigenmodeValidationRepository,
            )

            return CadEigenmodeValidationRepository(scene)
        if name == 'diffuseness_applicability':
            from .cad_diffuseness_applicability_repository import (
                CadDiffusenessApplicabilityRepository,
            )

            return CadDiffusenessApplicabilityRepository(scene)
        if name == 'coupled_decay':
            from .cad_coupled_decay_repository import (
                CadCoupledDecayRepository,
            )

            return CadCoupledDecayRepository(scene)
        if name == 'reflection_correspondence':
            from .cad_reflection_correspondence_repository import (
                CadReflectionCorrespondenceRepository,
            )

            return CadReflectionCorrespondenceRepository(scene)
        if name == 'modal_decay_view':
            from .cad_modal_decay_view_repository import (
                CadModalDecayViewRepository,
            )

            return CadModalDecayViewRepository(scene)
        # REV58-DISPLAYMEAS authorities.
        if name == 'pattern_generator_fidelity':
            from .cad_display_metrology_repository import (
                CadPatternGeneratorFidelityRepository,
            )

            return CadPatternGeneratorFidelityRepository(scene)
        if name == 'meter_match':
            from .cad_display_metrology_repository import (
                CadMeterMatchRepository,
            )

            return CadMeterMatchRepository(scene)
        if name == 'display_additivity':
            from .cad_display_metrology_repository import (
                CadDisplayAdditivityRepository,
            )

            return CadDisplayAdditivityRepository(scene)
        if name == 'temporal_display':
            from .cad_display_metrology_repository import (
                CadTemporalDisplayRepository,
            )

            return CadTemporalDisplayRepository(scene)
        if name == 'lut_closed_loop':
            from .cad_display_metrology_repository import (
                CadLutClosedLoopRepository,
            )

            return CadLutClosedLoopRepository(scene)
        # REV58-MEASELEC authorities.
        if name == 'interface_loopback':
            from .cad_interface_loopback_repository import (
                CadInterfaceLoopbackRepository,
            )

            return CadInterfaceLoopbackRepository(scene)
        if name == 'gain_structure':
            from .cad_gain_noise_structure_repository import (
                CadGainStructureRepository,
            )

            return CadGainStructureRepository(scene)
        if name == 'playback_dynamics':
            from .cad_playback_dynamics_repository import (
                CadPlaybackDynamicsRepository,
            )

            return CadPlaybackDynamicsRepository(scene)
        if name == 'active_crossover':
            from .cad_active_crossover_repository import (
                CadActiveCrossoverRepository,
            )

            return CadActiveCrossoverRepository(scene)
        if name == 'method_reproducibility':
            from .cad_method_reproducibility_repository import (
                CadMethodReproducibilityRepository,
            )

            return CadMethodReproducibilityRepository(scene)
        if name == 'apply_transaction':
            from .cad_apply_transaction_repository import (
                CadApplyTransactionRepository,
            )

            return CadApplyTransactionRepository(scene)
        if name == 'field_metric':
            from .cad_field_metric_repository import (
                CadFieldMetricRepository,
            )

            return CadFieldMetricRepository(scene)
        if name == 'signal_integrity':
            from .cad_signal_integrity_repository import (
                CadSignalIntegrityRepository,
            )

            return CadSignalIntegrityRepository(scene)
        if name == 'media_fidelity':
            from .cad_media_fidelity_repository import (
                CadMediaFidelityRepository,
            )

            return CadMediaFidelityRepository(scene)
        if name == 'power_evidence':
            from .cad_power_evidence_repository import (
                CadPowerEvidenceRepository,
            )

            return CadPowerEvidenceRepository(scene)
        if name == 'building_environment':
            from .cad_building_environment_repository import (
                CadBuildingEnvironmentRepository,
            )

            return CadBuildingEnvironmentRepository(scene)
        if name == 'measurement_setup':
            from .cad_measurement_setup_repository import (
                CadMeasurementSetupRepository,
            )

            return CadMeasurementSetupRepository(scene)
        # REV59-DEPS authorities.
        if name == 'authority_dependency':
            from .cad_authority_dependency_repository import (
                CadAuthorityDependencyRepository,
            )

            return CadAuthorityDependencyRepository(scene)
        if name == 'evidence_attestation':
            from .cad_evidence_attestation_repository import (
                CadEvidenceAttestationRepository,
            )

            return CadEvidenceAttestationRepository(scene)
        if name == 'archive_migration':
            from .cad_archive_migration_repository import (
                CadArchiveMigrationRepository,
            )

            return CadArchiveMigrationRepository(scene)
        if name == 'room_qualification':
            from .cad_room_qualification_repository import (
                CadRoomQualificationRepository,
            )

            return CadRoomQualificationRepository(scene)
        if name == 'numerical_transport':
            from .cad_numerical_transport_repository import (
                CadNumericalTransportRepository,
            )

            return CadNumericalTransportRepository(scene)
        if name == 'presentation_profile':
            from .cad_presentation_profile_repository import (
                CadPresentationProfileRepository,
            )

            return CadPresentationProfileRepository(scene)
        if name == 'code_policy':
            from .cad_code_policy_repository import (
                CadCodePolicyRepository,
            )

            return CadCodePolicyRepository(scene)
        if name == 'treatment_safety':
            from .cad_treatment_safety_repository import (
                CadTreatmentSafetyRepository,
            )

            return CadTreatmentSafetyRepository(scene)
        if name == 'listening_evidence':
            from .cad_listening_evidence_repository import (
                CadListeningEvidenceRepository,
            )

            return CadListeningEvidenceRepository(scene)
        if name == 'acoustic_metrology':
            from .cad_acoustic_metrology_repository import (
                CadAcousticMetrologyRepository,
            )

            return CadAcousticMetrologyRepository(scene)
        if name == 'field_metrology':
            from .cad_field_metrology_repository import (
                CadFieldMetrologyRepository,
            )

            return CadFieldMetrologyRepository(scene)
        if name == 'facility':
            from .cad_facility_repository import (
                CadFacilityRepository,
            )

            return CadFacilityRepository(scene)
        if name == 'display_authority':
            from .cad_display_authority_repository import (
                CadDisplayAuthorityRepository,
            )

            return CadDisplayAuthorityRepository(scene)
        if name == 'audio_perception':
            from .cad_audio_perception_repository import (
                CadAudioPerceptionRepository,
            )

            return CadAudioPerceptionRepository(scene)
        if name == 'signal_authority':
            from .cad_signal_authority_repository import (
                CadSignalAuthorityRepository,
            )

            return CadSignalAuthorityRepository(scene)
        if name == 'cadref':
            from .cad_cadref_repository import (
                CadCadRefRepository,
            )

            return CadCadRefRepository(scene)
        if name == 'verauto':
            from .cad_verification_repository import (
                CadVerAutoRepository,
            )

            return CadVerAutoRepository(scene)
                # REV59-UNITS authorities.
        if name == 'typed_quantity':
            from .cad_typed_quantity_repository import (
                CadTypedQuantityRepository,
            )

            return CadTypedQuantityRepository(scene)
        if name == 'assumption_ledger':
            from .cad_assumption_ledger_repository import (
                CadAssumptionLedgerRepository,
            )

            return CadAssumptionLedgerRepository(scene)
        if name == 'audibility':
            from .cad_audibility_repository import (
                CadAudibilityRepository,
            )

            return CadAudibilityRepository(scene)
        if name == 'diagnostic_hypothesis':
            from .cad_diagnostic_hypothesis_repository import (
                CadDiagnosticHypothesisRepository,
            )

            return CadDiagnosticHypothesisRepository(scene)
        if name == 'manifest_gates':
            from .cad_manifest_gate_repository import (
                CadManifestGateRepository,
            )

            return CadManifestGateRepository(scene)
        if name == 'loudspeaker_evidence':
            from .cad_loudspeaker_evidence_repository import (
                CadLoudspeakerEvidenceRepository,
            )

            return CadLoudspeakerEvidenceRepository(scene)
        if name == 'collaboration':
            from .cad_collaboration_repository import (
                CadCollaborationRepository,
            )

            return CadCollaborationRepository(scene)
        if name == 'material_condition':
            from .cad_material_condition_repository import (
                CadMaterialConditionRepository,
            )

            return CadMaterialConditionRepository(scene)
        if name == 'edge':
            from .cad_edge_repository import (
                CadEdgeAuthorityRepository,
            )

            return CadEdgeAuthorityRepository(scene)
        if name == 'deployment':
            from .cad_calibration_deployment_repository import (
                CadCalibrationDeploymentRepository,
            )

            return CadCalibrationDeploymentRepository(scene)
        if name == 'playback_electronics':
            from .cad_playback_electronics_repository import (
                CadPlaybackElectronicsRepository,
            )

            return CadPlaybackElectronicsRepository(scene)
        if name == 'supportability':
            from .cad_supportability_repository import (
                CadSupportabilityRepository,
            )

            return CadSupportabilityRepository(scene)
        if name == 'benchmark_qualification':
            from .cad_benchmark_qualification_repository import (
                CadBenchmarkQualificationRepository,
            )

            return CadBenchmarkQualificationRepository(scene)
        if name == 'live_spectrum':
            from .cad_live_spectrum_repository import (
                CadLiveSpectrumRepository,
            )

            return CadLiveSpectrumRepository(scene)
        if name == 'validation_uncertainty':
            from .cad_validation_uncertainty_repository import (
                CadValidationUncertaintyRepository,
            )

            return CadValidationUncertaintyRepository(scene)
        if name == 'solver_confidence':
            from .cad_solver_confidence_bound_repository import (
                CadSolverConfidenceBoundRepository,
            )

            return CadSolverConfidenceBoundRepository(scene)
        if name == 'transient_protection':
            from .cad_transient_protection_repository import (
                CadTransientProtectionRepository,
            )

            return CadTransientProtectionRepository(scene)
        if name == 'operational_energy':
            from .cad_operational_energy_repository import (
                CadOperationalEnergyRepository,
            )

            return CadOperationalEnergyRepository(scene)
        if name == 'owned_room_campaign':
            from .cad_owned_room_campaign_repository import (
                CadOwnedRoomCampaignRepository,
            )

            return CadOwnedRoomCampaignRepository(scene)
        if name == 'production_readiness':
            from .cad_production_readiness_repository import (
                CadProductionReadinessRepository,
            )

            return CadProductionReadinessRepository(scene)
        if name == 'hybrid_composition_validation':
            from .cad_hybrid_composition_validation_repository import (
                CadHybridCompositionValidationRepository,
            )

            return CadHybridCompositionValidationRepository(scene)
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
        'acceptance_run',
        'htdt_acceptance_runs',
        ('run_id', 'revision'),
        _get('acceptance', 'get_revision'),
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
        'standards_gap_matrix',
        'cad_standards_gap_matrices',
        ('matrix_id',),
        _get('standards', 'get_gap_matrix'),
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
        'prediction_measurement_registration',
        'cad_prediction_measurement_registrations',
        ('registration_id',),
        _get('prediction_measurement_registration', 'get'),
    ),
    _ReplayProbe(
        'prediction_measurement_residual_report',
        'cad_prediction_measurement_residual_reports',
        ('report_id',),
        _get('prediction_measurement_registration', 'get_report'),
    ),
    _ReplayProbe(
        'correction_qualification',
        'cad_correction_qualifications',
        ('qualification_id',),
        _get('correction_qualification', 'get'),
    ),
    _ReplayProbe(
        'measurement_uncertainty_budget',
        'cad_measurement_uncertainty_budgets',
        ('budget_id',),
        _get('measurement_evidence', 'get_uncertainty_budget'),
    ),
    _ReplayProbe(
        'measurement_significance_assessment',
        'cad_measurement_significance_assessments',
        ('assessment_id',),
        _get('measurement_evidence', 'get_significance_assessment'),
    ),
    _ReplayProbe(
        'measurement_state_policy',
        'cad_measurement_state_policies',
        ('policy_id',),
        _get('measurement_evidence', 'get_state_policy'),
    ),
    _ReplayProbe(
        'measurement_state_snapshot',
        'cad_measurement_state_snapshots',
        ('snapshot_id',),
        _get('measurement_evidence', 'get_state_snapshot'),
    ),
    _ReplayProbe(
        'measurement_state_verdict',
        'cad_measurement_state_verdicts',
        ('verdict_id',),
        _get('measurement_evidence', 'get_state_verdict'),
    ),
    _ReplayProbe(
        'measurement_transform',
        'cad_measurement_transforms',
        ('transform_id',),
        _get('measurement_evidence', 'get_transform'),
    ),
    _ReplayProbe(
        'decision_rule',
        'cad_decision_rule_specs',
        ('rule_id',),
        _get('decision_rule', 'get_rule'),
    ),
    _ReplayProbe(
        'decision_verdict',
        'cad_decision_verdicts',
        ('verdict_id',),
        _get('decision_rule', 'get_verdict'),
    ),
    _ReplayProbe(
        'uncertain_input_set',
        'cad_uncertain_input_sets',
        ('input_set_id',),
        _get('robust_design', 'get_input_set'),
    ),
    _ReplayProbe(
        'robust_design_assessment',
        'cad_robust_design_assessments',
        ('assessment_id',),
        _get('robust_design', 'get_assessment'),
    ),
    _ReplayProbe(
        'stimulus_asset',
        'cad_stimulus_assets',
        ('stimulus_id',),
        _get('stimulus_registry', 'get_asset'),
    ),
    _ReplayProbe(
        'stimulus_pin',
        'cad_stimulus_pins',
        ('pin_id',),
        _get('stimulus_registry', 'get_pin'),
    ),
    _ReplayProbe(
        'stimulus_eligibility',
        'cad_stimulus_eligibility',
        ('eligibility_id',),
        _get('stimulus_registry', 'get_eligibility'),
    ),
    _ReplayProbe(
        'bass_splice_evidence',
        'cad_bass_splice_evidence',
        ('evidence_id',),
        _get('bass_qualification', 'get_evidence'),
    ),
    _ReplayProbe(
        'bass_qualification',
        'cad_bass_qualifications',
        ('qualification_id',),
        _get('bass_qualification', 'get_qualification'),
    ),
    _ReplayProbe(
        'external_standard_document',
        'cad_external_standard_documents',
        ('registry_key',),
        _get('external_standards', 'get_document_by_key'),
    ),
    _ReplayProbe(
        'standard_lifecycle_observation',
        'cad_standard_lifecycle_observations',
        ('observation_id',),
        _get('external_standards', 'get_observation'),
    ),
    _ReplayProbe(
        'standard_profile_mapping',
        'cad_standard_profile_mappings',
        ('mapping_id',),
        _get('external_standards', 'get_mapping'),
    ),
    _ReplayProbe(
        'standard_evaluation_pin',
        'cad_standard_evaluation_pins',
        ('pin_id',),
        _get('external_standards', 'get_pin'),
    ),
    _ReplayProbe(
        'standard_revision_diff',
        'cad_standard_revision_diffs',
        ('diff_id',),
        _get('external_standards', 'get_revision_diff'),
    ),
    _ReplayProbe(
        'device_config_snapshot',
        'cad_device_config_snapshots',
        ('snapshot_id',),
        _get('device_snapshot', 'get_snapshot'),
    ),
    _ReplayProbe(
        'device_known_good_baseline',
        'cad_device_known_good_baselines',
        ('baseline_id',),
        _get('device_snapshot', 'get_baseline'),
    ),
    _ReplayProbe(
        'device_firmware_transition',
        'cad_device_firmware_transitions',
        ('transition_id',),
        _get('device_snapshot', 'get_firmware_transition'),
    ),
    _ReplayProbe(
        'device_restore_record',
        'cad_device_restore_records',
        ('restore_id',),
        _get('device_snapshot', 'get_restore_record'),
    ),
    _ReplayProbe(
        'device_backup_artifact',
        'cad_device_backup_artifacts',
        ('artifact_id',),
        _get('device_snapshot', 'get_backup_artifact_by_id'),
    ),
    _ReplayProbe(
        'device_replacement_assessment',
        'cad_device_replacement_assessments',
        ('assessment_id',),
        _get('device_snapshot', 'get_replacement_assessment'),
    ),
    _ReplayProbe(
        'spatial_campaign_design',
        'cad_spatial_campaign_designs',
        ('design_id',),
        _get('spatial_campaign', 'get_design'),
    ),
    _ReplayProbe(
        'spatial_campaign_evaluation',
        'cad_spatial_campaign_evaluations',
        ('evaluation_id',),
        _get('spatial_campaign', 'get_evaluation'),
    ),
    _ReplayProbe(
        'spatial_campaign_binding',
        'cad_spatial_campaign_bindings',
        ('binding_id',),
        _get('spatial_campaign', 'get_binding'),
    ),
    _ReplayProbe(
        'rp32_profile',
        'cad_rp32_profiles',
        ('profile_id',),
        _get('rp32_commissioning', 'get_profile'),
    ),
    _ReplayProbe(
        'rp32_reconciliation',
        'cad_rp32_reconciliations',
        ('reconciliation_id',),
        _get('rp32_commissioning', 'get_reconciliation'),
    ),
    _ReplayProbe(
        'rp32_readiness',
        'cad_rp32_readiness',
        ('assessment_id',),
        _get('rp32_commissioning', 'get_readiness'),
    ),
    _ReplayProbe(
        'rp32_verification_plan',
        'cad_rp32_verification_plans',
        ('plan_id',),
        _get('rp32_commissioning', 'get_plan'),
    ),
    _ReplayProbe(
        'rp32_verification_record',
        'cad_rp32_verification_records',
        ('record_id',),
        _get('rp32_commissioning', 'get_record'),
    ),
    _ReplayProbe(
        'rp32_report',
        'cad_rp32_reports',
        ('report_id',),
        _get('rp32_commissioning', 'get_report'),
    ),
    _ReplayProbe(
        'rp22_profile',
        'cad_rp22_profiles',
        ('profile_id', 'profile_version'),
        _get('rp22_profile', 'get_profile'),
    ),
    _ReplayProbe(
        'rp22_evaluation',
        'cad_rp22_evaluations',
        ('evaluation_id',),
        _get('rp22_profile', 'get_evaluation'),
    ),
    _ReplayProbe(
        'response_target',
        'cad_response_targets',
        ('profile_id', 'version'),
        _get('response_target', 'get_profile'),
    ),
    _ReplayProbe(
        'spectral_balance_evaluation',
        'cad_spectral_balance_evaluations',
        ('evaluation_id',),
        _get('response_target', 'get_evaluation'),
    ),
    _ReplayProbe(
        'multi_sub_candidate',
        'cad_multi_sub_candidates',
        ('candidate_id',),
        _get('multi_sub_optimization', 'get_candidate'),
    ),
    _ReplayProbe(
        'multi_sub_evaluation',
        'cad_multi_sub_evaluations',
        ('evaluation_id',),
        _get('multi_sub_optimization', 'get_evaluation'),
    ),
    _ReplayProbe(
        'multi_sub_qualification',
        'cad_multi_sub_qualifications',
        ('qualification_id',),
        _get('multi_sub_optimization', 'get_qualification'),
    ),
    _ReplayProbe(
        'multi_sub_stage_comparison',
        'cad_multi_sub_stage_comparisons',
        ('comparison_id',),
        _get('multi_sub_optimization', 'get_stage_comparison'),
    ),
    _ReplayProbe(
        'multi_sub_deployment',
        'cad_multi_sub_deployments',
        ('verification_id',),
        _get('multi_sub_optimization', 'get_deployment'),
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
    # ---- #534 presentation authority -----------------------------------
    _ReplayProbe(
        'presentation_session',
        'cad_presentation_sessions',
        ('session_id',),
        _get('presentation', 'verify_persisted_session'),
    ),
    _ReplayProbe(
        'presentation_proposal',
        'cad_presentation_proposals',
        ('proposal_id',),
        _get('presentation', 'verify_persisted_proposal'),
    ),
    _ReplayProbe(
        'presentation_sync_binding',
        'cad_presentation_sync_bindings',
        ('binding_id',),
        _get('presentation', 'verify_persisted_binding'),
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
    # ---- REV56-METRICS authorities ------------------------------------
    _ReplayProbe(
        'room_noise_metric_profile',
        'cad_room_noise_metric_profiles',
        ('profile_id',),
        _get('room_noise_metric', 'get_profile'),
    ),
    _ReplayProbe(
        'background_noise_measurement',
        'cad_background_noise_measurements',
        ('measurement_id',),
        _get('room_noise_metric', 'get_measurement'),
    ),
    _ReplayProbe(
        'noise_criterion_evaluation',
        'cad_noise_criterion_evaluations',
        ('evaluation_id',),
        _get('room_noise_metric', 'get_evaluation'),
    ),
    _ReplayProbe(
        'speech_intelligibility_profile',
        'cad_speech_intelligibility_profiles',
        ('profile_id',),
        _get('sti', 'get_profile'),
    ),
    _ReplayProbe(
        'sti_measurement',
        'cad_sti_measurements',
        ('measurement_id',),
        _get('sti', 'get_measurement'),
    ),
    _ReplayProbe(
        'sti_prediction',
        'cad_sti_predictions',
        ('prediction_id',),
        _get('sti', 'get_prediction'),
    ),
    _ReplayProbe(
        'dialogue_intelligibility_assessment',
        'cad_dialogue_intelligibility_assessments',
        ('assessment_id',),
        _get('sti', 'get_assessment'),
    ),
    _ReplayProbe(
        'content_loudness_profile',
        'cad_content_loudness_profiles',
        ('profile_id',),
        _get('loudness', 'get_profile'),
    ),
    _ReplayProbe(
        'programme_loudness_measurement',
        'cad_programme_loudness_measurements',
        ('measurement_id',),
        _get('loudness', 'get_measurement'),
    ),
    _ReplayProbe(
        'normalization_observation',
        'cad_normalization_observations',
        ('observation_id',),
        _get('loudness', 'get_observation'),
    ),
    _ReplayProbe(
        'playback_gain_state',
        'cad_playback_gain_states',
        ('state_id',),
        _get('loudness', 'get_gain_state'),
    ),
    _ReplayProbe(
        'loudness_matching_record',
        'cad_loudness_matching_records',
        ('record_id',),
        _get('loudness', 'get_matching_record'),
    ),
    # REV56-ELEC (#593 electrical qualification, #597 wiring traceability)
    _ReplayProbe(
        'electrical_qualification',
        'cad_electrical_qualifications',
        ('qualification_id',),
        _get('electrical_compatibility', 'get_qualification'),
    ),
    _ReplayProbe(
        'physical_interconnect',
        'cad_physical_interconnects',
        ('path_id', 'version'),
        _get('wiring_trace', 'get_interconnect'),
    ),
    _ReplayProbe(
        'wiring_verification',
        'cad_wiring_verifications',
        ('verification_id',),
        _get('wiring_trace', 'get_verification'),
    ),
    _ReplayProbe(
        'logical_physical_binding',
        'cad_logical_physical_bindings',
        ('binding_id',),
        _get('wiring_trace', 'get_binding'),
    ),
    # REV56-LIFECYCLE (#595 health/drift monitoring, #596 substitution
    # impact)
    _ReplayProbe(
        'monitoring_declaration',
        'cad_monitoring_declarations',
        ('declaration_id',),
        _get('health_drift', 'get_declaration'),
    ),
    _ReplayProbe(
        'lifecycle_observation',
        'cad_lifecycle_observations',
        ('observation_id',),
        _get('health_drift', 'get_observation'),
    ),
    _ReplayProbe(
        'change_event',
        'cad_change_events',
        ('event_id',),
        _get('health_drift', 'get_change_event'),
    ),
    _ReplayProbe(
        'trend_assessment',
        'cad_trend_assessments',
        ('assessment_id',),
        _get('health_drift', 'get_trend_assessment'),
    ),
    _ReplayProbe(
        'symptom_episode',
        'cad_symptom_episodes',
        ('episode_id',),
        _get('health_drift', 'get_symptom_episode'),
    ),
    _ReplayProbe(
        'drift_assessment',
        'cad_drift_assessments',
        ('assessment_id',),
        _get('health_drift', 'get_drift_assessment'),
    ),
    _ReplayProbe(
        'reverification_trigger',
        'cad_reverification_triggers',
        ('trigger_id',),
        _get('health_drift', 'get_trigger'),
    ),
    _ReplayProbe(
        'restore_confirmation',
        'cad_restore_confirmations',
        ('confirmation_id',),
        _get('health_drift', 'get_restore_confirmation'),
    ),
    _ReplayProbe(
        'substitution_proposal',
        'cad_substitution_proposals',
        ('proposal_id',),
        _get('substitution_impact', 'get_proposal'),
    ),
    _ReplayProbe(
        'change_impact_assessment',
        'cad_change_impact_assessments',
        ('assessment_id',),
        _get('substitution_impact', 'get_assessment'),
    ),
    _ReplayProbe(
        'substitution_decision',
        'cad_substitution_decisions',
        ('decision_id',),
        _get('substitution_impact', 'get_decision'),
    ),
    _ReplayProbe(
        'asbuilt_reconciliation',
        'cad_asbuilt_reconciliations',
        ('reconciliation_id',),
        _get('substitution_impact', 'get_reconciliation'),
    ),
    _ReplayProbe(
        'equipment_schedule_record',
        'cad_equipment_schedule_records',
        ('schedule_id',),
        _get('substitution_impact', 'get_schedule'),
    ),
    # REV56-TRANSPORT: #582/#583/#591
    _ReplayProbe(
        'av_latency_profile',
        'cad_av_latency_profiles',
        ('profile_id',),
        _get('av_latency', 'get_profile'),
    ),
    _ReplayProbe(
        'av_latency_path',
        'cad_av_latency_paths',
        ('path_id', 'version'),
        _get('av_latency', 'get_path'),
    ),
    _ReplayProbe(
        'av_latency_path_measurement',
        'cad_av_latency_path_measurements',
        ('measurement_id',),
        _get('av_latency', 'get_measurement'),
    ),
    _ReplayProbe(
        'av_latency_qualification',
        'cad_av_latency_qualifications',
        ('qualification_id',),
        _get('av_latency', 'get_qualification'),
    ),
    _ReplayProbe(
        'hdmi_signal_profile',
        'cad_hdmi_signal_profiles',
        ('profile_id',),
        _get('hdmi_verification', 'get_signal_profile'),
    ),
    _ReplayProbe(
        'hdmi_edid_artifact',
        'cad_hdmi_edid_artifacts',
        ('artifact_id',),
        _get('hdmi_verification', 'get_edid_artifact'),
    ),
    _ReplayProbe(
        'hdmi_hdcp_observation',
        'cad_hdmi_hdcp_observations',
        ('observation_id',),
        _get('hdmi_verification', 'get_hdcp_observation'),
    ),
    _ReplayProbe(
        'hdmi_link_observation',
        'cad_hdmi_link_observations',
        ('observation_id',),
        _get('hdmi_verification', 'get_link_observation'),
    ),
    _ReplayProbe(
        'hdmi_verification_record',
        'cad_hdmi_verification_records',
        ('record_id',),
        _get('hdmi_verification', 'get_verification_record'),
    ),
    _ReplayProbe(
        'hdmi_qualification',
        'cad_hdmi_qualifications',
        ('qualification_id',),
        _get('hdmi_verification', 'get_qualification'),
    ),
    _ReplayProbe(
        'rp28_profile',
        'cad_rp28_profiles',
        ('profile_id',),
        _get('hdmi_verification', 'get_rp28_profile'),
    ),
    _ReplayProbe(
        'network_av_path',
        'cad_network_av_paths',
        ('path_id', 'version'),
        _get('network_av', 'get_path'),
    ),
    _ReplayProbe(
        'network_media_flow',
        'cad_network_media_flows',
        ('flow_id',),
        _get('network_av', 'get_flow'),
    ),
    _ReplayProbe(
        'network_transport_observation',
        'cad_network_transport_observations',
        ('observation_id',),
        _get('network_av', 'get_transport_observation'),
    ),
    _ReplayProbe(
        'network_timing_observation',
        'cad_network_timing_observations',
        ('observation_id',),
        _get('network_av', 'get_timing_observation'),
    ),
    _ReplayProbe(
        'network_av_qualification',
        'cad_network_av_qualifications',
        ('qualification_id',),
        _get('network_av', 'get_qualification'),
    ),
    # ---- REV56-BUILDING authorities -----------------------------------
    _ReplayProbe(
        'isolation_element',
        'cad_isolation_elements',
        ('element_id',),
        _get('isolation_authority', 'get_element'),
    ),
    _ReplayProbe(
        'interroom_scenario',
        'cad_interroom_scenarios',
        ('scenario_id',),
        _get('isolation_authority', 'get_scenario'),
    ),
    _ReplayProbe(
        'interroom_field_measurement',
        'cad_interroom_field_measurements',
        ('measurement_id',),
        _get('isolation_authority', 'get_field_measurement'),
    ),
    _ReplayProbe(
        'isolation_calibration',
        'cad_isolation_calibrations',
        ('calibration_id',),
        _get('isolation_authority', 'get_calibration'),
    ),
    _ReplayProbe(
        'isolation_qualification',
        'cad_isolation_qualifications',
        ('qualification_id',),
        _get('isolation_authority', 'get_qualification'),
    ),
    _ReplayProbe(
        'mechanical_noise_test',
        'cad_mechanical_noise_tests',
        ('test_id',),
        _get('mechanical_noise', 'get_test'),
    ),
    _ReplayProbe(
        'rattle_event',
        'cad_rattle_events',
        ('event_id',),
        _get('mechanical_noise', 'get_event'),
    ),
    _ReplayProbe(
        'remediation_action',
        'cad_remediation_actions',
        ('action_id',),
        _get('mechanical_noise', 'get_remediation'),
    ),
    _ReplayProbe(
        'mechanical_noise_qualification',
        'cad_mechanical_noise_qualifications',
        ('qualification_id',),
        _get('mechanical_noise', 'get_qualification'),
    ),
    _ReplayProbe(
        'seat_acoustic_model',
        'cad_seat_acoustic_models',
        ('seat_model_id',),
        _get('seating_acoustics', 'get_seat_model'),
    ),
    _ReplayProbe(
        'occupancy_scenario',
        'cad_occupancy_scenarios',
        ('occupancy_scenario_id',),
        _get('seating_acoustics', 'get_occupancy_scenario'),
    ),
    _ReplayProbe(
        'clearance_evaluation',
        'cad_clearance_evaluations',
        ('evaluation_id',),
        _get('seating_acoustics', 'get_clearance_evaluation'),
    ),
    _ReplayProbe(
        'seating_commissioning_result',
        'cad_seating_commissioning_results',
        ('result_id',),
        _get('seating_acoustics', 'get_commissioning_result'),
    ),
    # REV56-OPS: #598 security authority
    _ReplayProbe(
        'security_asset',
        'cad_security_assets',
        ('asset_id',),
        _get('security_authority', 'get_asset'),
    ),
    _ReplayProbe(
        'security_credential',
        'cad_security_credentials',
        ('credential_id',),
        _get('security_authority', 'get_credential'),
    ),
    _ReplayProbe(
        'security_surface',
        'cad_security_surfaces',
        ('surface_id',),
        _get('security_authority', 'get_surface'),
    ),
    _ReplayProbe(
        'security_observation',
        'cad_security_observations',
        ('observation_id',),
        _get('security_authority', 'get_observation'),
    ),
    _ReplayProbe(
        'security_risk',
        'cad_security_risks',
        ('risk_id',),
        _get('security_authority', 'get_risk'),
    ),
    _ReplayProbe(
        'remote_service_authorization',
        'cad_remote_service_authorizations',
        ('authorization_id',),
        _get('security_authority', 'get_remote_authorization'),
    ),
    _ReplayProbe(
        'security_test_evidence',
        'cad_security_test_evidence',
        ('evidence_id',),
        _get('security_authority', 'get_test_evidence'),
    ),
    _ReplayProbe(
        'access_review',
        'cad_access_reviews',
        ('review_id',),
        _get('security_authority', 'get_access_review'),
    ),
    _ReplayProbe(
        'security_review',
        'cad_security_reviews',
        ('review_id',),
        _get('security_authority', 'get_review'),
    ),
    # REV56-OPS: #601 control-scenario qualification
    _ReplayProbe(
        'control_surface',
        'cad_control_surfaces',
        ('surface_id',),
        _get('control_scenario', 'get_surface'),
    ),
    _ReplayProbe(
        'control_scenario',
        'cad_control_scenarios',
        ('scenario_id',),
        _get('control_scenario', 'get_scenario'),
    ),
    _ReplayProbe(
        'control_scenario_run',
        'cad_control_scenario_runs',
        ('run_id',),
        _get('control_scenario', 'get_run'),
    ),
    _ReplayProbe(
        'control_scenario_qualification',
        'cad_control_qualifications',
        ('qualification_id',),
        _get('control_scenario', 'get_qualification'),
    ),
    # REV56-OPS: #602 safe-listening / test-exposure
    _ReplayProbe(
        'exposure_limit',
        'cad_exposure_limits',
        ('limit_id',),
        _get('safe_listening', 'get_limit'),
    ),
    _ReplayProbe(
        'spl_capability',
        'cad_spl_capabilities',
        ('capability_id',),
        _get('safe_listening', 'get_capability'),
    ),
    _ReplayProbe(
        'test_exposure_plan',
        'cad_test_exposure_plans',
        ('plan_id',),
        _get('safe_listening', 'get_plan'),
    ),
    _ReplayProbe(
        'exposure_gate',
        'cad_exposure_gates',
        ('gate_id',),
        _get('safe_listening', 'get_gate'),
    ),
    _ReplayProbe(
        'exposure_assessment',
        'cad_exposure_assessments',
        ('assessment_id',),
        _get('safe_listening', 'get_assessment'),
    ),
    _ReplayProbe(
        'ifc_import_artifact',
        'cad_ifc_import_artifacts',
        ('artifact_id',),
        _get('ifc_interop', 'get_import_artifact'),
    ),
    _ReplayProbe(
        'ifc_entity_mapping',
        'cad_ifc_entity_mappings',
        ('mapping_id',),
        _get('ifc_interop', 'get_entity_mapping'),
    ),
    _ReplayProbe(
        'ifc_revision_delta',
        'cad_ifc_revision_deltas',
        ('delta_id',),
        _get('ifc_interop', 'get_revision_delta'),
    ),
    _ReplayProbe(
        'ifc_intake_profile',
        'cad_ifc_intake_profiles',
        ('profile_id',),
        _get('ifc_interop', 'get_intake_profile'),
    ),
    _ReplayProbe(
        'ifc_intake_evaluation',
        'cad_ifc_intake_evaluations',
        ('evaluation_id',),
        _get('ifc_interop', 'get_intake_evaluation'),
    ),
    _ReplayProbe(
        'ifc_export_package',
        'cad_ifc_exports',
        ('export_id',),
        _get('ifc_interop', 'get_export_package'),
    ),
    _ReplayProbe(
        'performance_fact_profile',
        'cad_performance_fact_profiles',
        ('profile_id',),
        _get('performance_facts', 'get_profile'),
    ),
    _ReplayProbe(
        'performance_fact_product',
        'cad_performance_fact_products',
        ('product_id',),
        _get('performance_facts', 'get_product'),
    ),
    _ReplayProbe(
        'performance_fact',
        'cad_performance_facts',
        ('fact_id',),
        _get('performance_facts', 'get_fact'),
    ),
    _ReplayProbe(
        'performance_fact_import',
        'cad_performance_fact_imports',
        ('import_id',),
        _get('performance_facts', 'get_import'),
    ),
    _ReplayProbe(
        'performance_fact_evaluation',
        'cad_performance_fact_evaluations',
        ('evaluation_id',),
        _get('performance_facts', 'get_evaluation'),
    ),
    _ReplayProbe(
        'performance_fact_rebind',
        'cad_performance_fact_rebinds',
        ('rebind_id',),
        _get('performance_facts', 'get_rebind'),
    ),
    # REV56-INFRA: #587 rack/power/thermal
    _ReplayProbe(
        'rack_enclosure',
        'cad_rack_enclosures',
        ('rack_id',),
        _get('infrastructure', 'get_rack'),
    ),
    _ReplayProbe(
        'rack_device',
        'cad_rack_devices',
        ('device_id',),
        _get('infrastructure', 'get_rack_device'),
    ),
    _ReplayProbe(
        'branch_circuit',
        'cad_branch_circuits',
        ('circuit_id',),
        _get('infrastructure', 'get_circuit'),
    ),
    _ReplayProbe(
        'power_protection_device',
        'cad_power_protection_devices',
        ('protection_id',),
        _get('infrastructure', 'get_protection'),
    ),
    _ReplayProbe(
        'poe_budget',
        'cad_poe_budgets',
        ('poe_id',),
        _get('infrastructure', 'get_poe_budget'),
    ),
    _ReplayProbe(
        'infrastructure_scenario',
        'cad_infrastructure_scenarios',
        ('scenario_id',),
        _get('infrastructure', 'get_scenario'),
    ),
    _ReplayProbe(
        'rack_thermal_measurement',
        'cad_rack_thermal_measurements',
        ('measurement_id',),
        _get('infrastructure', 'get_thermal_measurement'),
    ),
    _ReplayProbe(
        'infrastructure_qualification',
        'cad_infrastructure_qualifications',
        ('qualification_id',),
        _get('infrastructure', 'get_qualification'),
    ),
    # REV56-INFRA: #603 immersive render path
    _ReplayProbe(
        'immersive_content',
        'cad_immersive_contents',
        ('content_id',),
        _get('render_path', 'get_content'),
    ),
    _ReplayProbe(
        'renderer_capability',
        'cad_renderer_capabilities',
        ('capability_id',),
        _get('render_path', 'get_capability'),
    ),
    _ReplayProbe(
        'speaker_layout',
        'cad_speaker_layouts',
        ('layout_id',),
        _get('render_path', 'get_layout'),
    ),
    _ReplayProbe(
        'render_session',
        'cad_render_sessions',
        ('session_id',),
        _get('render_path', 'get_session'),
    ),
    _ReplayProbe(
        'render_output_observation',
        'cad_render_output_observations',
        ('observation_id',),
        _get('render_path', 'get_observation'),
    ),
    _ReplayProbe(
        'render_path_qualification',
        'cad_render_path_qualifications',
        ('qualification_id',),
        _get('render_path', 'get_qualification'),
    ),
    # REV56-INFRA: #606 hum/buzz grounding-EMC
    _ReplayProbe(
        'electrical_noise_observation',
        'cad_electrical_noise_observations',
        ('observation_id',),
        _get('electrical_noise', 'get_observation'),
    ),
    _ReplayProbe(
        'audio_interconnect',
        'cad_audio_interconnects',
        ('interconnect_id',),
        _get('electrical_noise', 'get_interconnect'),
    ),
    _ReplayProbe(
        'noise_isolation_test',
        'cad_noise_isolation_tests',
        ('test_id',),
        _get('electrical_noise', 'get_isolation_test'),
    ),
    _ReplayProbe(
        'humbuzz_diagnostic',
        'cad_humbuzz_diagnostics',
        ('diagnostic_id',),
        _get('electrical_noise', 'get_diagnostic'),
    ),
    _ReplayProbe(
        'noise_mitigation',
        'cad_noise_mitigations',
        ('attempt_id',),
        _get('electrical_noise', 'get_mitigation'),
    ),
    _ReplayProbe(
        'humbuzz_verdict',
        'cad_humbuzz_verdicts',
        ('verdict_id',),
        _get('electrical_noise', 'get_verdict'),
    ),
    # REV57-METRO: #609 timebase / clock authority
    _ReplayProbe(
        'timebase_clock_domain',
        'cad_timebase_clock_domains',
        ('clock_domain_id',),
        _get('timebase_authority', 'get_clock_domain'),
    ),
    _ReplayProbe(
        'measurement_timebase',
        'cad_measurement_timebases',
        ('timebase_id',),
        _get('timebase_authority', 'get_timebase'),
    ),
    _ReplayProbe(
        'timebase_capability_assessment',
        'cad_timebase_capability_assessments',
        ('assessment_id',),
        _get('timebase_authority', 'get_assessment'),
    ),
    # REV57-METRO: #610 evidence bundle / integrity manifest
    _ReplayProbe(
        'evidence_bundle',
        'cad_evidence_bundles',
        ('bundle_id',),
        _get('evidence_bundle', 'get_bundle'),
    ),
    _ReplayProbe(
        'evidence_artifact',
        'cad_evidence_artifacts',
        ('artifact_id',),
        _get('evidence_bundle', 'get_artifact'),
    ),
    _ReplayProbe(
        'evidence_derivation_edge',
        'cad_evidence_derivation_edges',
        ('edge_id',),
        _get('evidence_bundle', 'get_edge'),
    ),
    _ReplayProbe(
        'evidence_attestation',
        'cad_evidence_attestations',
        ('attestation_id',),
        _get('evidence_bundle', 'get_attestation'),
    ),
    _ReplayProbe(
        'evidence_bundle_validation',
        'cad_evidence_bundle_validations',
        ('verdict_id',),
        _get('evidence_bundle', 'get_verdict'),
    ),
    # REV57-METRO: #611 instrument calibration lifecycle
    _ReplayProbe(
        'instrument_instance',
        'cad_instrument_instances',
        ('instrument_id',),
        _get('calibration_lifecycle', 'get_instrument'),
    ),
    _ReplayProbe(
        'calibration_event',
        'cad_calibration_events',
        ('calibration_id',),
        _get('calibration_lifecycle', 'get_calibration'),
    ),
    _ReplayProbe(
        'calibration_interval_policy',
        'cad_calibration_interval_policies',
        ('policy_id',),
        _get('calibration_lifecycle', 'get_policy'),
    ),
    _ReplayProbe(
        'instrument_verification_check',
        'cad_instrument_verification_checks',
        ('check_id',),
        _get('calibration_lifecycle', 'get_check'),
    ),
    _ReplayProbe(
        'instrument_service_event',
        'cad_instrument_service_events',
        ('event_id',),
        _get('calibration_lifecycle', 'get_service_event'),
    ),
    _ReplayProbe(
        'instrument_fitness_assessment',
        'cad_instrument_fitness_assessments',
        ('assessment_id',),
        _get('calibration_lifecycle', 'get_assessment'),
    ),
    _ReplayProbe(
        'out_of_tolerance_review',
        'cad_out_of_tolerance_reviews',
        ('review_id',),
        _get('calibration_lifecycle', 'get_review'),
    ),
    # REV57-PHYS: #613 geometry survey authority
    _ReplayProbe(
        'geo_survey_instrument',
        'cad_geo_survey_instruments',
        ('instrument_id',),
        _get('geometry_survey', 'get_instrument'),
    ),
    _ReplayProbe(
        'geo_survey_campaign',
        'cad_geo_survey_campaigns',
        ('campaign_id',),
        _get('geometry_survey', 'get_campaign'),
    ),
    _ReplayProbe(
        'geo_element_evidence',
        'cad_geo_element_evidence',
        ('element_id',),
        _get('geometry_survey', 'get_element'),
    ),
    _ReplayProbe(
        'geo_control_measurement',
        'cad_geo_control_measurements',
        ('control_id',),
        _get('geometry_survey', 'get_control'),
    ),
    _ReplayProbe(
        'geo_reconciliation',
        'cad_geo_reconciliations',
        ('reconciliation_id',),
        _get('geometry_survey', 'get_reconciliation'),
    ),
    _ReplayProbe(
        'geo_task_requirement',
        'cad_geo_task_requirements',
        ('task_id',),
        _get('geometry_survey', 'get_task_requirement'),
    ),
    _ReplayProbe(
        'geo_qualification',
        'cad_geo_qualifications',
        ('qualification_id',),
        _get('geometry_survey', 'get_qualification'),
    ),
    # REV57-PHYS: #614 installed-source boundary authority
    _ReplayProbe(
        'src_meas_condition',
        'cad_src_meas_conditions',
        ('condition_id',),
        _get('installed_source', 'get_condition'),
    ),
    _ReplayProbe(
        'src_mounting_condition',
        'cad_src_mounting_conditions',
        ('mounting_id',),
        _get('installed_source', 'get_mounting'),
    ),
    _ReplayProbe(
        'src_boundary_correction',
        'cad_src_boundary_corrections',
        ('correction_id',),
        _get('installed_source', 'get_correction'),
    ),
    _ReplayProbe(
        'src_measurement',
        'cad_src_measurements',
        ('measurement_id',),
        _get('installed_source', 'get_measurement'),
    ),
    _ReplayProbe(
        'src_boundary_qualification',
        'cad_src_boundary_qualifications',
        ('qualification_id',),
        _get('installed_source', 'get_qualification'),
    ),
    # REV57-PHYS: #615 porous absorber authority
    _ReplayProbe(
        'pam_parameter_evidence',
        'cad_pam_parameter_evidence',
        ('evidence_id',),
        _get('porous_absorber', 'get_parameter'),
    ),
    _ReplayProbe(
        'pam_material_model',
        'cad_pam_material_models',
        ('model_id',),
        _get('porous_absorber', 'get_model'),
    ),
    _ReplayProbe(
        'pam_buildup',
        'cad_pam_buildups',
        ('buildup_id',),
        _get('porous_absorber', 'get_buildup'),
    ),
    _ReplayProbe(
        'pam_prediction',
        'cad_pam_predictions',
        ('prediction_id',),
        _get('porous_absorber', 'get_prediction'),
    ),
    _ReplayProbe(
        'pam_fit_comparison',
        'cad_pam_fit_comparisons',
        ('comparison_id',),
        _get('porous_absorber', 'get_comparison'),
    ),
    # REV57-PROJ: #619 spatial projection-image qualification
    _ReplayProbe(
        'spatial_measurement_plan',
        'cad_spatial_measurement_plans',
        ('plan_id',),
        _get('spatial_image', 'get_plan'),
    ),
    _ReplayProbe(
        'spatial_measurement_set',
        'cad_spatial_measurement_sets',
        ('set_id',),
        _get('spatial_image', 'get_measurement_set'),
    ),
    _ReplayProbe(
        'spatial_derived_map',
        'cad_spatial_derived_maps',
        ('map_id',),
        _get('spatial_image', 'get_derived_map'),
    ),
    _ReplayProbe(
        'spatial_uniformity_evaluation',
        'cad_spatial_uniformity_evaluations',
        ('evaluation_id',),
        _get('spatial_image', 'get_evaluation'),
    ),
    # REV57-PROJ: #622 projection image-geometry / masking
    _ReplayProbe(
        'presentation_geometry_binding',
        'cad_presentation_geometry_bindings',
        ('binding_id',),
        _get('projection_geometry', 'get_binding'),
    ),
    _ReplayProbe(
        'image_geometry_measurement',
        'cad_image_geometry_measurements',
        ('measurement_id',),
        _get('projection_geometry', 'get_measurement'),
    ),
    _ReplayProbe(
        'lens_memory_recall',
        'cad_lens_memory_recalls',
        ('recall_id',),
        _get('projection_geometry', 'get_lens_recall'),
    ),
    _ReplayProbe(
        'geometry_evaluation',
        'cad_geometry_evaluations',
        ('evaluation_id',),
        _get('projection_geometry', 'get_evaluation'),
    ),
    # REV57-PROJ: #624 hush-box / enclosure co-design
    _ReplayProbe(
        'projector_install_constraints',
        'cad_projector_install_constraints',
        ('constraint_id',),
        _get('hushbox', 'get_constraints'),
    ),
    _ReplayProbe(
        'projector_enclosure_plan',
        'cad_projector_enclosure_plans',
        ('plan_id',),
        _get('hushbox', 'get_plan'),
    ),
    _ReplayProbe(
        'enclosure_operating_observation',
        'cad_enclosure_operating_observations',
        ('observation_id',),
        _get('hushbox', 'get_operating_observation'),
    ),
    _ReplayProbe(
        'enclosure_acoustic_observation',
        'cad_enclosure_acoustic_observations',
        ('acoustic_id',),
        _get('hushbox', 'get_acoustic_observation'),
    ),
    _ReplayProbe(
        'enclosure_qualification',
        'cad_enclosure_qualifications',
        ('qualification_id',),
        _get('hushbox', 'get_qualification'),
    ),
    # REV57-PROJ: #627 optical-radiation safety
    _ReplayProbe(
        'projector_safety_identity',
        'cad_projector_safety_identities',
        ('identity_id',),
        _get('optical_safety', 'get_safety_identity'),
    ),
    _ReplayProbe(
        'manufacturer_safety_constraints',
        'cad_manufacturer_safety_constraints',
        ('constraint_id',),
        _get('optical_safety', 'get_safety_constraints'),
    ),
    _ReplayProbe(
        'projector_placement',
        'cad_projector_placements',
        ('placement_id',),
        _get('optical_safety', 'get_placement'),
    ),
    _ReplayProbe(
        'optical_safety_evaluation',
        'cad_optical_safety_evaluations',
        ('evaluation_id',),
        _get('optical_safety', 'get_evaluation'),
    ),
    # REV57-DISP: #625 direct-view display authority
    _ReplayProbe(
        'dv_display_state',
        'cad_dv_display_states',
        ('display_state_id',),
        _get('direct_view_display', 'get_display_state'),
    ),
    _ReplayProbe(
        'dv_stimulus_context',
        'cad_dv_stimulus_contexts',
        ('stimulus_context_id',),
        _get('direct_view_display', 'get_stimulus_context'),
    ),
    _ReplayProbe(
        'dv_photometric_measurement',
        'cad_dv_photometric_measurements',
        ('measurement_id',),
        _get('direct_view_display', 'get_measurement'),
    ),
    _ReplayProbe(
        'dv_temporal_observation',
        'cad_dv_temporal_observations',
        ('observation_id',),
        _get('direct_view_display', 'get_temporal_observation'),
    ),
    _ReplayProbe(
        'dv_spatial_measurement',
        'cad_dv_spatial_measurements',
        ('spatial_id',),
        _get('direct_view_display', 'get_spatial_measurement'),
    ),
    _ReplayProbe(
        'dv_angle_measurement',
        'cad_dv_angle_measurements',
        ('angle_id',),
        _get('direct_view_display', 'get_angle_measurement'),
    ),
    _ReplayProbe(
        'dv_qualification',
        'cad_dv_qualifications',
        ('qualification_id',),
        _get('direct_view_display', 'get_qualification'),
    ),
    # REV57-DISP: #626 observer-metamerism authority
    _ReplayProbe(
        'om_spectral_state',
        'cad_om_spectral_states',
        ('spectral_state_id',),
        _get('observer_metamerism', 'get_spectral_state'),
    ),
    _ReplayProbe(
        'om_observer_profile',
        'cad_om_observer_profiles',
        ('profile_id',),
        _get('observer_metamerism', 'get_profile'),
    ),
    _ReplayProbe(
        'om_evaluation',
        'cad_om_evaluations',
        ('evaluation_id',),
        _get('observer_metamerism', 'get_evaluation'),
    ),
    _ReplayProbe(
        'om_perceptual_match',
        'cad_om_perceptual_matches',
        ('match_id',),
        _get('observer_metamerism', 'get_perceptual_match'),
    ),
    _ReplayProbe(
        'om_qualification',
        'cad_om_qualifications',
        ('qualification_id',),
        _get('observer_metamerism', 'get_qualification'),
    ),
    # REV57-DISP: #633 viewing-environment authority
    _ReplayProbe(
        've_observation',
        'cad_ve_observations',
        ('observation_id',),
        _get('viewing_environment', 'get_observation'),
    ),
    _ReplayProbe(
        've_geometry_observation',
        'cad_ve_geometry_observations',
        ('geometry_id',),
        _get('viewing_environment', 'get_geometry'),
    ),
    _ReplayProbe(
        've_lighting_scene',
        'cad_ve_lighting_scenes',
        ('scene_id',),
        _get('viewing_environment', 'get_scene'),
    ),
    _ReplayProbe(
        've_qualification',
        'cad_ve_qualifications',
        ('qualification_id',),
        _get('viewing_environment', 'get_qualification'),
    ),
    # REV57-AUD: #621 acoustic channel-identity / polarity verification
    _ReplayProbe(
        'channel_identity_chain',
        'cad_channel_identity_chains',
        ('chain_id',),
        _get('channel_identity', 'get_chain'),
    ),
    _ReplayProbe(
        'acoustic_endpoint_observation',
        'cad_acoustic_endpoint_observations',
        ('observation_id',),
        _get('channel_identity', 'get_observation'),
    ),
    _ReplayProbe(
        'channel_identity_test',
        'cad_channel_identity_tests',
        ('test_id',),
        _get('channel_identity', 'get_test'),
    ),
    _ReplayProbe(
        'polarity_verification_record',
        'cad_polarity_verification_records',
        ('record_id',),
        _get('channel_identity', 'get_polarity_record'),
    ),
    _ReplayProbe(
        'channel_identity_evaluation',
        'cad_channel_identity_evaluations',
        ('evaluation_id',),
        _get('channel_identity', 'get_evaluation'),
    ),
    # REV57-AUD: #634 listener-area coverage / acoustic-aim
    _ReplayProbe(
        'acoustic_aim_state',
        'cad_acoustic_aim_states',
        ('aim_id',),
        _get('coverage_aim', 'get_aim_state'),
    ),
    _ReplayProbe(
        'coverage_listener_area',
        'cad_coverage_listener_areas',
        ('area_id',),
        _get('coverage_aim', 'get_listener_area'),
    ),
    _ReplayProbe(
        'coverage_prediction',
        'cad_coverage_predictions',
        ('prediction_id',),
        _get('coverage_aim', 'get_prediction'),
    ),
    _ReplayProbe(
        'coverage_measurement_set',
        'cad_coverage_measurement_sets',
        ('set_id',),
        _get('coverage_aim', 'get_measurement_set'),
    ),
    _ReplayProbe(
        'coverage_qualification',
        'cad_coverage_qualifications',
        ('qualification_id',),
        _get('coverage_aim', 'get_qualification'),
    ),
    # REV57-AUD: #628 installed loudspeaker instance variation
    _ReplayProbe(
        'instance_acoustic_evidence',
        'cad_instance_acoustic_evidence',
        ('evidence_id',),
        _get('instance_variation', 'get_evidence'),
    ),
    _ReplayProbe(
        'model_instance_delta',
        'cad_model_instance_deltas',
        ('delta_id',),
        _get('instance_variation', 'get_delta'),
    ),
    _ReplayProbe(
        'matched_set_declaration',
        'cad_matched_set_declarations',
        ('set_id',),
        _get('instance_variation', 'get_matched_set'),
    ),
    _ReplayProbe(
        'matched_set_qualification',
        'cad_matched_set_qualifications',
        ('qualification_id',),
        _get('instance_variation', 'get_qualification'),
    ),
    # REV57-AUD: #632 media-playback capability qualification
    _ReplayProbe(
        'playback_stack_identity',
        'cad_playback_stack_identities',
        ('stack_id',),
        _get('media_playback', 'get_stack'),
    ),
    _ReplayProbe(
        'media_profile_requirement',
        'cad_media_profile_requirements',
        ('requirement_id',),
        _get('media_playback', 'get_requirement'),
    ),
    _ReplayProbe(
        'playback_capability_record',
        'cad_playback_capability_records',
        ('record_id',),
        _get('media_playback', 'get_record'),
    ),
    _ReplayProbe(
        'playback_operation_run',
        'cad_playback_operation_runs',
        ('run_id',),
        _get('media_playback', 'get_run'),
    ),
    _ReplayProbe(
        'playback_qualification',
        'cad_playback_qualifications',
        ('qualification_id',),
        _get('media_playback', 'get_qualification'),
    ),
    # REV57-INST: #616/#618/#631/#612 authorities
    _ReplayProbe(
        'hvac_scenario',
        'cad_hvac_ventilation_scenarios',
        ('scenario_id',),
        _get('hvac', 'get_scenario'),
    ),
    _ReplayProbe(
        'hvac_path',
        'cad_hvac_path_declarations',
        ('path_id',),
        _get('hvac', 'get_path'),
    ),
    _ReplayProbe(
        'hvac_component_evidence',
        'cad_hvac_component_evidence',
        ('evidence_id',),
        _get('hvac', 'get_component_evidence'),
    ),
    _ReplayProbe(
        'hvac_field_observation',
        'cad_hvac_field_observations',
        ('observation_id',),
        _get('hvac', 'get_observation'),
    ),
    _ReplayProbe(
        'hvac_qualification',
        'cad_hvac_qualifications',
        ('qualification_id',),
        _get('hvac', 'get_qualification'),
    ),
    # REV57-INST: #618 playback reference-calibration authority
    _ReplayProbe(
        'ref_cal_profile',
        'cad_ref_cal_profiles',
        ('profile_id',),
        _get('playback_reference', 'get_profile'),
    ),
    _ReplayProbe(
        'ref_cal_stimulus',
        'cad_ref_cal_stimuli',
        ('stimulus_id',),
        _get('playback_reference', 'get_stimulus'),
    ),
    _ReplayProbe(
        'ref_cal_observation',
        'cad_ref_cal_observations',
        ('observation_id',),
        _get('playback_reference', 'get_observation'),
    ),
    _ReplayProbe(
        'ref_cal_qualification',
        'cad_ref_cal_qualifications',
        ('qualification_id',),
        _get('playback_reference', 'get_qualification'),
    ),
    # REV57-INST: #631 as-built treatment qualification authority
    _ReplayProbe(
        'treatment_install_spec',
        'cad_treatment_install_specs',
        ('spec_id',),
        _get('treatment_asbuilt', 'get_spec'),
    ),
    _ReplayProbe(
        'treatment_asbuilt_observation',
        'cad_treatment_asbuilt_observations',
        ('observation_id',),
        _get('treatment_asbuilt', 'get_observation'),
    ),
    _ReplayProbe(
        'treatment_inspection',
        'cad_treatment_inspections',
        ('inspection_id',),
        _get('treatment_asbuilt', 'get_inspection'),
    ),
    _ReplayProbe(
        'treatment_qualification',
        'cad_treatment_qualifications',
        ('qualification_id',),
        _get('treatment_asbuilt', 'get_qualification'),
    ),
    # REV57-INST: #612 tactile/seat-vibration authority
    _ReplayProbe(
        'tactile_vibration_path',
        'cad_tactile_vibration_paths',
        ('path_id',),
        _get('tactile_vibration', 'get_path'),
    ),
    _ReplayProbe(
        'tactile_vibration_measurement',
        'cad_tactile_vibration_measurements',
        ('measurement_id',),
        _get('tactile_vibration', 'get_measurement'),
    ),
    _ReplayProbe(
        'tactile_profile',
        'cad_tactile_profiles',
        ('profile_id',),
        _get('tactile_vibration', 'get_profile'),
    ),
    _ReplayProbe(
        'tactile_vibration_qualification',
        'cad_tactile_vibration_qualifications',
        ('qualification_id',),
        _get('tactile_vibration', 'get_qualification'),
    ),
    # REV57-MOUNT: #620 AV mounting / structural-support evidence
    _ReplayProbe(
        'mount_assembly',
        'cad_mount_assemblies',
        ('assembly_id',),
        _get('mounting_support', 'get_assembly'),
    ),
    _ReplayProbe(
        'mount_load_evidence',
        'cad_mount_load_evidence',
        ('evidence_id',),
        _get('mounting_support', 'get_load_evidence'),
    ),
    _ReplayProbe(
        'mount_support_element',
        'cad_mount_support_elements',
        ('element_id',),
        _get('mounting_support', 'get_support_element'),
    ),
    _ReplayProbe(
        'mount_manufacturer_requirement',
        'cad_mount_manufacturer_requirements',
        ('requirement_id',),
        _get('mounting_support', 'get_manufacturer_requirement'),
    ),
    _ReplayProbe(
        'mount_structural_approval',
        'cad_mount_structural_approvals',
        ('approval_id',),
        _get('mounting_support', 'get_approval'),
    ),
    _ReplayProbe(
        'mount_inspection',
        'cad_mount_inspection_records',
        ('inspection_id',),
        _get('mounting_support', 'get_inspection'),
    ),
    _ReplayProbe(
        'mount_qualification',
        'cad_mount_qualifications',
        ('qualification_id',),
        _get('mounting_support', 'get_qualification'),
    ),
    # REV58-MEASCHAIN: #695 measurement-chain linearity/overload
    _ReplayProbe(
        'measchain_linearity_profile',
        'cad_measchain_linearity_profiles',
        ('profile_id',),
        _get('measchain_linearity', 'get_profile'),
    ),
    _ReplayProbe(
        'measchain_overload_observation',
        'cad_measchain_overload_observations',
        ('observation_id',),
        _get('measchain_linearity', 'get_observation'),
    ),
    _ReplayProbe(
        'measchain_qualification',
        'cad_measchain_qualifications',
        ('qualification_id',),
        _get('measchain_linearity', 'get_qualification'),
    ),
    # REV58-MEASCHAIN: #697 swept-sine deconvolution/harmonic separation
    _ReplayProbe(
        'sweep_deconvolution_spec',
        'cad_sweep_deconvolution_specs',
        ('spec_id',),
        _get('sweep_deconvolution', 'get_spec'),
    ),
    _ReplayProbe(
        'harmonic_impulse_component',
        'cad_harmonic_impulse_components',
        ('component_id',),
        _get('sweep_deconvolution', 'get_component'),
    ),
    _ReplayProbe(
        'recovered_impulse_response',
        'cad_recovered_impulse_responses',
        ('ir_id',),
        _get('sweep_deconvolution', 'get_recovered_ir'),
    ),
    _ReplayProbe(
        'linear_ir_capability',
        'cad_linear_ir_capabilities',
        ('capability_id',),
        _get('sweep_deconvolution', 'get_capability'),
    ),
    # REV58-MEASCHAIN: #668 room-acoustic excitation source
    _ReplayProbe(
        'excitation_source_profile',
        'cad_excitation_source_profiles',
        ('profile_id',),
        _get('excitation_source', 'get_profile'),
    ),
    _ReplayProbe(
        'source_orientation_capture',
        'cad_source_orientation_captures',
        ('capture_id',),
        _get('excitation_source', 'get_capture'),
    ),
    _ReplayProbe(
        'measurement_source_qualification',
        'cad_measurement_source_qualifications',
        ('qualification_id',),
        _get('excitation_source', 'get_qualification'),
    ),
    # REV58-DSPDECAY: #679 DSP filter realization
    _ReplayProbe(
        'dsp_realization_profile',
        'cad_dsp_realization_profiles',
        ('profile_id',),
        _get('dsp_realization', 'get_profile'),
    ),
    _ReplayProbe(
        'dsp_stage_record',
        'cad_dsp_stage_records',
        ('stage_id',),
        _get('dsp_realization', 'get_stage'),
    ),
    _ReplayProbe(
        'dsp_parameter_mapping',
        'cad_dsp_parameter_mappings',
        ('mapping_id',),
        _get('dsp_realization', 'get_mapping'),
    ),
    _ReplayProbe(
        'dsp_realization_qualification',
        'cad_dsp_realization_qualifications',
        ('qualification_id',),
        _get('dsp_realization', 'get_qualification'),
    ),
    # REV58-DSPDECAY: #676 decay-curve noise/truncation processing
    _ReplayProbe(
        'decay_processing_profile',
        'cad_decay_processing_profiles',
        ('profile_id',),
        _get('decay_processing', 'get_profile'),
    ),
    _ReplayProbe(
        'decay_noise_estimate',
        'cad_decay_noise_estimates',
        ('estimate_id',),
        _get('decay_processing', 'get_noise_estimate'),
    ),
    _ReplayProbe(
        'rir_truncation_decision',
        'cad_decay_truncation_decisions',
        ('decision_id',),
        _get('decay_processing', 'get_truncation_decision'),
    ),
    _ReplayProbe(
        'decay_edc_artifact',
        'cad_decay_edc_artifacts',
        ('artifact_id',),
        _get('decay_processing', 'get_edc_artifact'),
    ),
    _ReplayProbe(
        'decay_fit_record',
        'cad_decay_fit_records',
        ('record_id',),
        _get('decay_processing', 'get_fit_record'),
    ),
    # REV58-DSPDECAY: #705 acoustic impedance physical realizability
    _ReplayProbe(
        'boundary_evidence_record',
        'cad_boundary_evidence_records',
        ('record_id',),
        _get('boundary_realizability', 'get_evidence'),
    ),
    _ReplayProbe(
        'boundary_rational_fit',
        'cad_boundary_rational_fits',
        ('fit_id',),
        _get('boundary_realizability', 'get_rational_fit'),
    ),
    _ReplayProbe(
        'td_impedance_realization',
        'cad_td_impedance_realizations',
        ('realization_id',),
        _get('boundary_realizability', 'get_td_realization'),
    ),
    _ReplayProbe(
        'boundary_realizability_assessment',
        'cad_boundary_realizability_assessments',
        ('assessment_id',),
        _get('boundary_realizability', 'get_assessment'),
    ),
    # REV58-NUMERIC: #683 wave-solver numerical fidelity
    _ReplayProbe(
        'wave_fidelity_profile',
        'cad_wave_fidelity_profiles',
        ('profile_id',),
        _get('wave_fidelity', 'get_profile'),
    ),
    _ReplayProbe(
        'wave_convergence_record',
        'cad_wave_convergence_records',
        ('convergence_id',),
        _get('wave_fidelity', 'get_convergence'),
    ),
    _ReplayProbe(
        'wave_fidelity_qualification',
        'cad_wave_fidelity_qualifications',
        ('qualification_id',),
        _get('wave_fidelity', 'get_qualification'),
    ),
    # REV58-NUMERIC: #685 geometrical-acoustics numerical fidelity
    _ReplayProbe(
        'geometric_fidelity_profile',
        'cad_geometric_fidelity_profiles',
        ('profile_id',),
        _get('geometric_fidelity', 'get_profile'),
    ),
    _ReplayProbe(
        'ray_sampling_convergence',
        'cad_ray_sampling_convergences',
        ('convergence_id',),
        _get('geometric_fidelity', 'get_convergence'),
    ),
    _ReplayProbe(
        'path_enumeration_qualification',
        'cad_path_enumeration_qualifications',
        ('qualification_id',),
        _get('geometric_fidelity', 'get_enumeration'),
    ),
    _ReplayProbe(
        'geometric_fidelity_qualification',
        'cad_geometric_fidelity_qualifications',
        ('qualification_id',),
        _get('geometric_fidelity', 'get_qualification'),
    ),
    # REV58-NUMERIC: #687 wave↔geometrical hybrid handoff
    _ReplayProbe(
        'hybrid_composition_profile',
        'cad_hybrid_composition_profiles',
        ('profile_id',),
        _get('hybrid_handoff', 'get_profile'),
    ),
    _ReplayProbe(
        'hybrid_transition_qualification',
        'cad_hybrid_transition_qualifications',
        ('qualification_id',),
        _get('hybrid_handoff', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #654 acoustic-reference origin / phase center
    _ReplayProbe(
        'source_origin_profile',
        'cad_source_origin_profiles',
        ('profile_id',),
        _get('source_origin', 'get_profile'),
    ),
    _ReplayProbe(
        'source_origin_qualification',
        'cad_source_origin_qualifications',
        ('qualification_id',),
        _get('source_origin', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #655 source near/far-field applicability
    _ReplayProbe(
        'source_field_profile',
        'cad_source_field_profiles',
        ('profile_id',),
        _get('source_field_applicability', 'get_profile'),
    ),
    _ReplayProbe(
        'source_field_qualification',
        'cad_source_field_qualifications',
        ('qualification_id',),
        _get('source_field_applicability', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #656 directivity angular resolution
    _ReplayProbe(
        'directivity_sampling_profile',
        'cad_directivity_sampling_profiles',
        ('profile_id',),
        _get('directivity_resolution', 'get_profile'),
    ),
    _ReplayProbe(
        'directivity_interpolation_record',
        'cad_directivity_interpolation_records',
        ('record_id',),
        _get('directivity_resolution', 'get_interpolation_record'),
    ),
    _ReplayProbe(
        'directivity_direction_qualification',
        'cad_directivity_direction_qualifications',
        ('qualification_id',),
        _get('directivity_resolution', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #690 multi-source correlation / coherence
    _ReplayProbe(
        'source_coherence_profile',
        'cad_source_coherence_profiles',
        ('profile_id',),
        _get('source_coherence', 'get_profile'),
    ),
    _ReplayProbe(
        'source_combination_qualification',
        'cad_source_combination_qualifications',
        ('qualification_id',),
        _get('source_coherence', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #684 geometric surface-scattering model
    _ReplayProbe(
        'scattering_model_profile',
        'cad_scattering_model_profiles',
        ('profile_id',),
        _get('scattering_model', 'get_profile'),
    ),
    _ReplayProbe(
        'scattering_model_qualification',
        'cad_scattering_model_qualifications',
        ('qualification_id',),
        _get('scattering_model', 'get_qualification'),
    ),
    # REV58-AUDIOMODEL: #681 edge diffraction model
    _ReplayProbe(
        'diffraction_model_profile',
        'cad_diffraction_model_profiles',
        ('profile_id',),
        _get('edge_diffraction', 'get_profile'),
    ),
    _ReplayProbe(
        'diffraction_benchmark_result',
        'cad_diffraction_benchmark_results',
        ('result_id',),
        _get('edge_diffraction', 'get_benchmark_result'),
    ),
    _ReplayProbe(
        'diffraction_qualification',
        'cad_diffraction_qualifications',
        ('qualification_id',),
        _get('edge_diffraction', 'get_qualification'),
    ),
    # REV58-IDENT: #691 typed logarithmic quantity / dB reference
    _ReplayProbe(
        'log_quantity',
        'cad_log_quantities',
        ('quantity_id',),
        _get('logarithmic_quantity', 'get_quantity'),
    ),
    _ReplayProbe(
        'log_calibration_bridge',
        'cad_log_calibration_bridges',
        ('bridge_id',),
        _get('logarithmic_quantity', 'get_bridge'),
    ),
    _ReplayProbe(
        'log_operation',
        'cad_log_operations',
        ('operation_id',),
        _get('logarithmic_quantity', 'get_operation'),
    ),
    # REV58-IDENT: #689 calibration-parameter identifiability
    _ReplayProbe(
        'calib_parameter_record',
        'cad_calib_parameter_records',
        ('parameter_id',),
        _get('parameter_identifiability', 'get_parameter'),
    ),
    _ReplayProbe(
        'ident_sensitivity_evidence',
        'cad_ident_sensitivity_evidence',
        ('sensitivity_id',),
        _get('parameter_identifiability', 'get_sensitivity'),
    ),
    _ReplayProbe(
        'ident_correlation_evidence',
        'cad_ident_correlation_evidence',
        ('correlation_id',),
        _get('parameter_identifiability', 'get_correlation'),
    ),
    _ReplayProbe(
        'ident_equivalent_set',
        'cad_ident_equivalent_sets',
        ('set_id',),
        _get('parameter_identifiability', 'get_equivalent_set'),
    ),
    _ReplayProbe(
        'identifiability_assessment',
        'cad_identifiability_assessments',
        ('assessment_id',),
        _get('parameter_identifiability', 'get_assessment'),
    ),
    # REV58-IDENT: #698 validation sample-dependence / leakage
    _ReplayProbe(
        'validation_statistical_design',
        'cad_validation_statistical_designs',
        ('design_id',),
        _get('validation_statistics', 'get_design'),
    ),
    _ReplayProbe(
        'dependence_model',
        'cad_dependence_models',
        ('dependence_id',),
        _get('validation_statistics', 'get_dependence'),
    ),
    _ReplayProbe(
        'dataset_role_assignment',
        'cad_dataset_role_assignments',
        ('assignment_id',),
        _get('validation_statistics', 'get_role_assignment'),
    ),
    _ReplayProbe(
        'benchmark_exposure',
        'cad_benchmark_exposures',
        ('exposure_id',),
        _get('validation_statistics', 'get_exposure'),
    ),
    _ReplayProbe(
        'challenge_qualification',
        'cad_challenge_qualifications',
        ('qualification_id',),
        _get('validation_statistics', 'get_qualification'),
    ),
    # REV58-VALIDMETH: #675 optimizer algorithm qualification
    _ReplayProbe(
        'optimization_problem',
        'cad_optimization_problems',
        ('problem_id',),
        _get('optimizer_qualification', 'get_problem'),
    ),
    _ReplayProbe(
        'optimizer_run_profile',
        'cad_optimizer_run_profiles',
        ('profile_id',),
        _get('optimizer_qualification', 'get_profile'),
    ),
    _ReplayProbe(
        'optimizer_qualification',
        'cad_optimizer_qualifications',
        ('qualification_id',),
        _get('optimizer_qualification', 'get_qualification'),
    ),
    _ReplayProbe(
        'pareto_assessment',
        'cad_pareto_assessments',
        ('assessment_id',),
        _get('optimizer_qualification', 'get_pareto_assessment'),
    ),
    # REV58-VALIDMETH: #674 acoustic eigenmode validation
    _ReplayProbe(
        'mode_pairing',
        'cad_mode_pairings',
        ('pairing_id',),
        _get('eigenmode_validation', 'get_pairing'),
    ),
    _ReplayProbe(
        'eigenmode_verdict',
        'cad_eigenmode_verdicts',
        ('verdict_id',),
        _get('eigenmode_validation', 'get_verdict'),
    ),
    # REV58-VALIDMETH: #673 sound-field diffuseness applicability
    _ReplayProbe(
        'diffuseness_assessment',
        'cad_diffuseness_assessments',
        ('assessment_id',),
        _get('diffuseness_applicability', 'get_assessment'),
    ),
    _ReplayProbe(
        'statistical_applicability_declaration',
        'cad_statistical_applicability_declarations',
        ('declaration_id',),
        _get('diffuseness_applicability', 'get_declaration'),
    ),
    # REV58-VALIDMETH: #671 coupled-room multi-slope decay
    _ReplayProbe(
        'multi_slope_fit',
        'cad_multi_slope_fits',
        ('fit_id',),
        _get('coupled_decay', 'get_fit'),
    ),
    _ReplayProbe(
        'single_slope_assessment',
        'cad_single_slope_assessments',
        ('assessment_id',),
        _get('coupled_decay', 'get_assessment'),
    ),
    _ReplayProbe(
        'coupled_decay_qualification',
        'cad_coupled_decay_qualifications',
        ('qualification_id',),
        _get('coupled_decay', 'get_qualification'),
    ),
    # REV58-VALIDMETH: #677 early-reflection correspondence
    _ReplayProbe(
        'reflection_pairing',
        'cad_reflection_pairings',
        ('pairing_id',),
        _get('reflection_correspondence', 'get_pairing'),
    ),
    _ReplayProbe(
        'reflection_correspondence_set',
        'cad_reflection_correspondence_sets',
        ('set_id',),
        _get('reflection_correspondence', 'get_set'),
    ),
    _ReplayProbe(
        'reflection_correspondence_verdict',
        'cad_reflection_correspondence_verdicts',
        ('verdict_id',),
        _get('reflection_correspondence', 'get_verdict'),
    ),
    # REV58-VALIDMETH: #706 time-frequency modal decay
    _ReplayProbe(
        'modal_decay_observation',
        'cad_modal_decay_observations',
        ('observation_id',),
        _get('modal_decay_view', 'get_observation'),
    ),
    _ReplayProbe(
        'modal_decay_qualification',
        'cad_modal_decay_qualifications',
        ('qualification_id',),
        _get('modal_decay_view', 'get_qualification'),
    ),
    # REV59-DEPS: #729 authority dependency / staleness graph
    _ReplayProbe(
        'dependency_edge_declaration',
        'cad_dependency_edge_declarations',
        ('edge_id',),
        _get('authority_dependency', 'get_edge'),
    ),
    _ReplayProbe(
        'dependency_change_event',
        'cad_dependency_change_events',
        ('event_id',),
        _get('authority_dependency', 'get_change_event'),
    ),
    _ReplayProbe(
        'dependency_rule_profile',
        'cad_dependency_rule_profiles',
        ('profile_id',),
        _get('authority_dependency', 'get_rule_profile'),
    ),
    _ReplayProbe(
        'staleness_assessment',
        'cad_staleness_assessments',
        ('assessment_id',),
        _get('authority_dependency', 'get_assessment'),
    ),
    _ReplayProbe(
        'revalidation_plan',
        'cad_revalidation_plans',
        ('plan_id',),
        _get('authority_dependency', 'get_plan'),
    ),
    # REV59-DEPS: #725 evidence attestation / trusted timestamp
    _ReplayProbe(
        'signed_manifest',
        'cad_signed_manifests',
        ('manifest_id',),
        _get('evidence_attestation', 'get_manifest'),
    ),
    _ReplayProbe(
        'manifest_attestation',
        'cad_manifest_attestations',
        ('attestation_id',),
        _get('evidence_attestation', 'get_attestation'),
    ),
    _ReplayProbe(
        'attestation_verification',
        'cad_attestation_verifications',
        ('verification_id',),
        _get('evidence_attestation', 'get_verification'),
    ),
    # REV59-DEPS: #718 project archival / schema-migration
    _ReplayProbe(
        'archive_snapshot',
        'cad_archive_snapshots',
        ('archive_id',),
        _get('archive_migration', 'get_snapshot'),
    ),
    _ReplayProbe(
        'archive_verification',
        'cad_archive_verifications',
        ('verification_id',),
        _get('archive_migration', 'get_archive_verification'),
    ),
    _ReplayProbe(
        'migration_record',
        'cad_migration_records',
        ('migration_id',),
        _get('archive_migration', 'get_migration'),
    ),
    _ReplayProbe(
        'migration_verification',
        'cad_migration_verifications',
        ('verification_id',),
        _get('archive_migration', 'get_migration_verification'),
    ),
    # REV58-DISPLAYMEAS: #682 pattern-generator fidelity
    _ReplayProbe(
        'pg_generator_instance',
        'cad_pg_generator_instances',
        ('generator_id',),
        _get('pattern_generator_fidelity', 'get_generator'),
    ),
    _ReplayProbe(
        'pg_requested_patch',
        'cad_pg_requested_patches',
        ('patch_id',),
        _get('pattern_generator_fidelity', 'get_patch'),
    ),
    _ReplayProbe(
        'pg_delivered_observation',
        'cad_pg_delivered_observations',
        ('observation_id',),
        _get('pattern_generator_fidelity', 'get_observation'),
    ),
    _ReplayProbe(
        'pg_fidelity_qualification',
        'cad_pg_fidelity_qualifications',
        ('qualification_id',),
        _get('pattern_generator_fidelity', 'get_qualification'),
    ),
    # REV58-DISPLAYMEAS: #680 probe matching / spectral mismatch
    _ReplayProbe(
        'mm_match_profile',
        'cad_mm_match_profiles',
        ('match_id',),
        _get('meter_match', 'get_profile'),
    ),
    _ReplayProbe(
        'mm_match_observation',
        'cad_mm_match_observations',
        ('observation_id',),
        _get('meter_match', 'get_observation'),
    ),
    _ReplayProbe(
        'mm_verification',
        'cad_mm_verifications',
        ('verification_id',),
        _get('meter_match', 'get_verification'),
    ),
    _ReplayProbe(
        'mm_applicability',
        'cad_mm_applicability',
        ('applicability_id',),
        _get('meter_match', 'get_applicability'),
    ),
    # REV58-DISPLAYMEAS: #686 additivity / separation / volumetric
    _ReplayProbe(
        'da_additivity_observation',
        'cad_da_additivity_observations',
        ('observation_id',),
        _get('display_additivity', 'get_observation'),
    ),
    _ReplayProbe(
        'da_separation_assessment',
        'cad_da_separation_assessments',
        ('assessment_id',),
        _get('display_additivity', 'get_separation'),
    ),
    _ReplayProbe(
        'da_volumetric_characterisation',
        'cad_da_volumetric_characterisations',
        ('characterisation_id',),
        _get('display_additivity', 'get_characterisation'),
    ),
    _ReplayProbe(
        'da_holdout_verification',
        'cad_da_holdout_verifications',
        ('verification_id',),
        _get('display_additivity', 'get_holdout'),
    ),
    _ReplayProbe(
        'da_model_eligibility',
        'cad_da_model_eligibility',
        ('eligibility_id',),
        _get('display_additivity', 'get_eligibility'),
    ),
    _ReplayProbe(
        'da_characterisation_plan',
        'cad_da_characterisation_plans',
        ('plan_id',),
        _get('display_additivity', 'get_plan'),
    ),
    # REV58-DISPLAYMEAS: #647 temporal display fidelity
    _ReplayProbe(
        'td_state',
        'cad_td_states',
        ('state_id',),
        _get('temporal_display', 'get_state'),
    ),
    _ReplayProbe(
        'td_step_response',
        'cad_td_step_responses',
        ('measurement_id',),
        _get('temporal_display', 'get_step_response'),
    ),
    _ReplayProbe(
        'td_motion_measurement',
        'cad_td_motion_measurements',
        ('measurement_id',),
        _get('temporal_display', 'get_motion_measurement'),
    ),
    _ReplayProbe(
        'td_flicker_measurement',
        'cad_td_flicker_measurements',
        ('measurement_id',),
        _get('temporal_display', 'get_flicker_measurement'),
    ),
    _ReplayProbe(
        'td_retention_observation',
        'cad_td_retention_observations',
        ('observation_id',),
        _get('temporal_display', 'get_retention_observation'),
    ),
    _ReplayProbe(
        'td_qualification',
        'cad_td_qualifications',
        ('qualification_id',),
        _get('temporal_display', 'get_qualification'),
    ),
    # REV58-DISPLAYMEAS: #666 LUT closed-loop calibration
    _ReplayProbe(
        'lut_artifact',
        'cad_lut_artifacts',
        ('artifact_id',),
        _get('lut_closed_loop', 'get_artifact'),
    ),
    _ReplayProbe(
        'lut_generation_record',
        'cad_lut_generation_records',
        ('generation_id',),
        _get('lut_closed_loop', 'get_generation'),
    ),
    _ReplayProbe(
        'lut_preflight_verification',
        'cad_lut_preflight_verifications',
        ('preflight_id',),
        _get('lut_closed_loop', 'get_preflight'),
    ),
    _ReplayProbe(
        'lut_deployment',
        'cad_lut_deployments',
        ('deployment_id',),
        _get('lut_closed_loop', 'get_deployment'),
    ),
    _ReplayProbe(
        'lut_post_verification',
        'cad_lut_post_verifications',
        ('post_verification_id',),
        _get('lut_closed_loop', 'get_post_verification'),
    ),
    _ReplayProbe(
        'lut_qualification',
        'cad_lut_qualifications',
        ('qualification_id',),
        _get('lut_closed_loop', 'get_qualification'),
    ),
    # REV58-MEASELEC: #699 interface transfer/loopback calibration
    _ReplayProbe(
        'interface_loopback_observation',
        'cad_interface_loopback_observations',
        ('observation_id',),
        _get('interface_loopback', 'get_observation'),
    ),
    _ReplayProbe(
        'interface_transfer_calibration',
        'cad_interface_transfer_calibrations',
        ('calibration_id',),
        _get('interface_loopback', 'get_calibration'),
    ),
    _ReplayProbe(
        'interface_correction_qualification',
        'cad_interface_correction_qualifications',
        ('qualification_id',),
        _get('interface_loopback', 'get_qualification'),
    ),
    # REV58-MEASELEC: #651 gain structure / noise floor
    _ReplayProbe(
        'signal_level_reference',
        'cad_signal_level_references',
        ('reference_id',),
        _get('gain_structure', 'get_level_reference'),
    ),
    _ReplayProbe(
        'noise_floor_observation',
        'cad_noise_floor_observations',
        ('observation_id',),
        _get('gain_structure', 'get_noise_observation'),
    ),
    _ReplayProbe(
        'clipping_margin',
        'cad_clipping_margins',
        ('margin_id',),
        _get('gain_structure', 'get_clipping_margin'),
    ),
    _ReplayProbe(
        'gain_structure_qualification',
        'cad_gain_structure_qualifications',
        ('qualification_id',),
        _get('gain_structure', 'get_qualification'),
    ),
    # REV58-MEASELEC: #649 playback dynamics / limiter
    _ReplayProbe(
        'playback_dynamics_state',
        'cad_playback_dynamics_states',
        ('state_id',),
        _get('playback_dynamics', 'get_state'),
    ),
    _ReplayProbe(
        'level_sweep_observation',
        'cad_level_sweep_observations',
        ('observation_id',),
        _get('playback_dynamics', 'get_level_sweep'),
    ),
    _ReplayProbe(
        'playback_dynamics_qualification',
        'cad_playback_dynamics_qualifications',
        ('qualification_id',),
        _get('playback_dynamics', 'get_qualification'),
    ),
    # REV58-MEASELEC: #665 active multi-way crossover
    _ReplayProbe(
        'multiway_speaker_definition',
        'cad_multiway_speaker_definitions',
        ('definition_id',),
        _get('active_crossover', 'get_definition'),
    ),
    _ReplayProbe(
        'active_crossover_plan',
        'cad_active_crossover_plans',
        ('plan_id',),
        _get('active_crossover', 'get_plan'),
    ),
    _ReplayProbe(
        'driver_alignment_measurement',
        'cad_driver_alignment_measurements',
        ('measurement_id',),
        _get('active_crossover', 'get_alignment_measurement'),
    ),
    _ReplayProbe(
        'active_crossover_qualification',
        'cad_active_crossover_qualifications',
        ('qualification_id',),
        _get('active_crossover', 'get_qualification'),
    ),
    # REV58-MEASELEC: #693 measurement-method reproducibility
    _ReplayProbe(
        'method_procedure',
        'cad_method_procedures',
        ('procedure_id',),
        _get('method_reproducibility', 'get_procedure'),
    ),
    _ReplayProbe(
        'reproducibility_campaign',
        'cad_reproducibility_campaigns',
        ('campaign_id',),
        _get('method_reproducibility', 'get_campaign'),
    ),
    _ReplayProbe(
        'method_precision_model',
        'cad_method_precision_models',
        ('model_id',),
        _get('method_reproducibility', 'get_precision_model'),
    ),
    _ReplayProbe(
        'reproducibility_qualification',
        'cad_reproducibility_qualifications',
        ('qualification_id',),
        _get('method_reproducibility', 'get_qualification'),
    ),
    # REV59-APPLY: #723 device apply transaction / rollback
    _ReplayProbe(
        'apply_capability_profile',
        'cad_apply_capability_profiles',
        ('profile_id',),
        _get('apply_transaction', 'get_profile'),
    ),
    _ReplayProbe(
        'apply_plan',
        'cad_apply_plans',
        ('plan_id',),
        _get('apply_transaction', 'get_plan'),
    ),
    _ReplayProbe(
        'apply_write_record',
        'cad_apply_write_records',
        ('write_id',),
        _get('apply_transaction', 'get_write'),
    ),
    _ReplayProbe(
        'apply_verification',
        'cad_apply_verifications',
        ('verification_id',),
        _get('apply_transaction', 'get_verification'),
    ),
    _ReplayProbe(
        'apply_rollback_plan',
        'cad_apply_rollback_plans',
        ('rollback_plan_id',),
        _get('apply_transaction', 'get_rollback_plan'),
    ),
    _ReplayProbe(
        'apply_rollback_execution',
        'cad_apply_rollback_executions',
        ('execution_id',),
        _get('apply_transaction', 'get_rollback_execution'),
    ),
    _ReplayProbe(
        'apply_transaction',
        'cad_apply_transactions',
        ('transaction_id',),
        _get('apply_transaction', 'get_transaction'),
    ),
    # REV59-BENCH2: #763/#764/#755/#770 field/metric/budget authorities
    _ReplayProbe(
        'fractional_octave_profile',
        'cad_fractional_octave_profiles',
        ('profile_id',),
        _get('field_metric', 'get_band_profile'),
    ),
    _ReplayProbe(
        'band_integration',
        'cad_band_integrations',
        ('record_id',),
        _get('field_metric', 'get_band_integration'),
    ),
    _ReplayProbe(
        'echo_density_profile',
        'cad_echo_density_profiles',
        ('profile_id',),
        _get('field_metric', 'get_echo_profile'),
    ),
    _ReplayProbe(
        'mixing_time_estimate',
        'cad_mixing_time_estimates',
        ('estimate_id',),
        _get('field_metric', 'get_mixing_estimate'),
    ),
    _ReplayProbe(
        'late_field_assessment',
        'cad_late_field_assessments',
        ('assessment_id',),
        _get('field_metric', 'get_late_assessment'),
    ),
    _ReplayProbe(
        'interpolation_profile',
        'cad_interpolation_profiles',
        ('profile_id',),
        _get('field_metric', 'get_interp_profile'),
    ),
    _ReplayProbe(
        'field_surface_record',
        'cad_field_surface_records',
        ('record_id',),
        _get('field_metric', 'get_field_surface'),
    ),
    _ReplayProbe(
        'solver_budget_profile',
        'cad_solver_budget_profiles',
        ('profile_id',),
        _get('field_metric', 'get_budget_profile'),
    ),
    _ReplayProbe(
        'compute_observation',
        'cad_compute_observations',
        ('observation_id',),
        _get('field_metric', 'get_compute_observation'),
    ),
    _ReplayProbe(
        'accuracy_cost_envelope',
        'cad_accuracy_cost_envelopes',
        ('envelope_id',),
        _get('field_metric', 'get_cost_envelope'),
    ),
    # REV59-DIGCHAIN: #745/#744/#739/#650 signal-integrity authorities
    _ReplayProbe(
        'jitter_profile',
        'cad_jitter_profiles',
        ('profile_id',),
        _get('signal_integrity', 'get_jitter_profile'),
    ),
    _ReplayProbe(
        'jitter_observation',
        'cad_jitter_observations',
        ('observation_id',),
        _get('signal_integrity', 'get_jitter_observation'),
    ),
    _ReplayProbe(
        'jitter_transfer',
        'cad_jitter_transfer_measurements',
        ('measurement_id',),
        _get('signal_integrity', 'get_jitter_transfer'),
    ),
    _ReplayProbe(
        'converter_jitter_susceptibility',
        'cad_converter_jitter_susceptibility',
        ('susceptibility_id',),
        _get('signal_integrity', 'get_converter_susceptibility'),
    ),
    _ReplayProbe(
        'dither_profile',
        'cad_dither_profiles',
        ('profile_id',),
        _get('signal_integrity', 'get_dither_profile'),
    ),
    _ReplayProbe(
        'digital_path_transform',
        'cad_digital_path_transforms',
        ('transform_id',),
        _get('signal_integrity', 'get_path_transform'),
    ),
    _ReplayProbe(
        'playback_src_profile',
        'cad_playback_src_profiles',
        ('profile_id',),
        _get('signal_integrity', 'get_src_profile'),
    ),
    _ReplayProbe(
        'src_qualification',
        'cad_src_qualifications',
        ('qualification_id',),
        _get('signal_integrity', 'get_src_qualification'),
    ),
    _ReplayProbe(
        'clock_domain_crossing',
        'cad_clock_domain_crossings',
        ('crossing_id',),
        _get('signal_integrity', 'get_clock_crossing'),
    ),
    _ReplayProbe(
        'interchannel_leakage',
        'cad_interchannel_leakage_measurements',
        ('measurement_id',),
        _get('signal_integrity', 'get_leakage_measurement'),
    ),
    _ReplayProbe(
        'channel_separation',
        'cad_channel_separation_qualifications',
        ('qualification_id',),
        _get('signal_integrity', 'get_separation_qualification'),
    ),    # REV59-VIDMETA: #759/#756/#760/#753/#747 media fidelity authorities
    _ReplayProbe(
        'projector_light_profile',
        'cad_projector_light_profiles',
        ('profile_id',),
        _get('media_fidelity', 'get_light_profile'),
    ),
    _ReplayProbe(
        'temporal_contrast_measure',
        'cad_temporal_contrast_measures',
        ('measurement_id',),
        _get('media_fidelity', 'get_contrast_measure'),
    ),
    _ReplayProbe(
        'dynamic_contrast_qualification',
        'cad_dynamic_contrast_qualifications',
        ('qualification_id',),
        _get('media_fidelity', 'get_contrast_qualification'),
    ),
    _ReplayProbe(
        'light_capability',
        'cad_light_measurement_capabilities',
        ('capability_id',),
        _get('media_fidelity', 'get_light_capability'),
    ),
    _ReplayProbe(
        'luminance_observation',
        'cad_low_luminance_observations',
        ('observation_id',),
        _get('media_fidelity', 'get_luminance_observation'),
    ),
    _ReplayProbe(
        'display_boundary_profile',
        'cad_display_boundary_profiles',
        ('profile_id',),
        _get('media_fidelity', 'get_boundary_profile'),
    ),
    _ReplayProbe(
        'front_stage_variant',
        'cad_front_stage_variants',
        ('variant_id',),
        _get('media_fidelity', 'get_front_stage_variant'),
    ),
    _ReplayProbe(
        'codec_chain_profile',
        'cad_codec_chain_profiles',
        ('profile_id',),
        _get('media_fidelity', 'get_codec_chain'),
    ),
    _ReplayProbe(
        'quality_method_profile',
        'cad_quality_method_profiles',
        ('method_id',),
        _get('media_fidelity', 'get_quality_method'),
    ),
    _ReplayProbe(
        'codec_fidelity_observation',
        'cad_codec_fidelity_observations',
        ('observation_id',),
        _get('media_fidelity', 'get_fidelity_observation'),
    ),    # REV59-POWEREV: #736/#738/#752 power/EMC authorities
    _ReplayProbe(
        'power_sequencing_profile',
        'cad_power_sequencing_profiles',
        ('profile_id',),
        _get('power_evidence', 'get_sequencing_profile'),
    ),
    _ReplayProbe(
        'power_sequence_event',
        'cad_power_sequence_events',
        ('event_id',),
        _get('power_evidence', 'get_sequence_event'),
    ),
    _ReplayProbe(
        'ups_transition',
        'cad_ups_transition_records',
        ('record_id',),
        _get('power_evidence', 'get_ups_transition'),
    ),
    _ReplayProbe(
        'power_quality_measure',
        'cad_power_quality_measurements',
        ('measurement_id',),
        _get('power_evidence', 'get_pq_measurement'),
    ),
    _ReplayProbe(
        'power_quality_qualification',
        'cad_power_quality_qualifications',
        ('qualification_id',),
        _get('power_evidence', 'get_pq_qualification'),
    ),
    _ReplayProbe(
        'emc_product_profile',
        'cad_emc_product_profiles',
        ('profile_id',),
        _get('power_evidence', 'get_emc_profile'),
    ),
    _ReplayProbe(
        'emc_symptom',
        'cad_emc_symptom_records',
        ('record_id',),
        _get('power_evidence', 'get_emc_symptom'),
    ),    # REV59-BUILDENV: #751/#740/#750 building-environment authorities
    _ReplayProbe(
        'product_safety_profile',
        'cad_product_safety_profiles',
        ('profile_id',),
        _get('building_environment', 'get_safety_profile'),
    ),
    _ReplayProbe(
        'occupied_iaq_observation',
        'cad_occupied_iaq_observations',
        ('observation_id',),
        _get('building_environment', 'get_iaq_observation'),
    ),
    _ReplayProbe(
        'occupied_iaq_qualification',
        'cad_occupied_iaq_qualifications',
        ('qualification_id',),
        _get('building_environment', 'get_iaq_qualification'),
    ),
    _ReplayProbe(
        'voc_emission_profile',
        'cad_voc_emission_profiles',
        ('profile_id',),
        _get('building_environment', 'get_voc_profile'),
    ),    # REV59-ACOUST2: #743/#749/#765 measurement-setup authorities
    _ReplayProbe(
        'measurement_fixture_profile',
        'cad_measurement_fixture_profiles',
        ('profile_id',),
        _get('measurement_setup', 'get_fixture_profile'),
    ),
    _ReplayProbe(
        'fixture_scattering_observation',
        'cad_fixture_scattering_observations',
        ('observation_id',),
        _get('measurement_setup', 'get_scattering_observation'),
    ),
    _ReplayProbe(
        'spectral_estimator_profile',
        'cad_spectral_estimator_profiles',
        ('profile_id',),
        _get('measurement_setup', 'get_estimator_profile'),
    ),
    _ReplayProbe(
        'spectral_resolution_claim',
        'cad_spectral_resolution_claims',
        ('claim_id',),
        _get('measurement_setup', 'get_resolution_claim'),
    ),
    _ReplayProbe(
        'external_evidence_source',
        'cad_external_evidence_sources',
        ('source_id',),
        _get('measurement_setup', 'get_evidence_source'),
    ),
    _ReplayProbe(
        'evidence_supersession_record',
        'cad_evidence_supersession_records',
        ('record_id',),
        _get('measurement_setup', 'get_supersession_record'),
    ),
    # REV59-ROOMQ: #761/#704/#707 room-qualification authorities
    _ReplayProbe(
        'sound_strength_observation',
        'cad_sound_strength_observations',
        ('observation_id',),
        _get('room_qualification', 'get_g_observation'),
    ),
    _ReplayProbe(
        'sound_strength_qualification',
        'cad_sound_strength_qualifications',
        ('qualification_id',),
        _get('room_qualification', 'get_g_qualification'),
    ),
    _ReplayProbe(
        'resonant_absorber_profile',
        'cad_resonant_absorber_profiles',
        ('profile_id',),
        _get('room_qualification', 'get_resonant_profile'),
    ),
    _ReplayProbe(
        'resonant_performance_record',
        'cad_resonant_performance_records',
        ('record_id',),
        _get('room_qualification', 'get_resonant_record'),
    ),
    _ReplayProbe(
        'service_envelope_profile',
        'cad_service_envelope_profiles',
        ('profile_id',),
        _get('room_qualification', 'get_service_envelope'),
    ),
    _ReplayProbe(
        'service_access_observation',
        'cad_service_access_observations',
        ('observation_id',),
        _get('room_qualification', 'get_service_observation'),
    ),
    # REV59-QUALNUM: #703/#716/#717
    _ReplayProbe(
        'numerical_repro_profile',
        'cad_numerical_repro_profiles',
        ('profile_id',),
        _get('numerical_transport', 'get_repro_profile'),
    ),
    _ReplayProbe(
        'stochastic_realization',
        'cad_stochastic_realizations',
        ('record_id',),
        _get('numerical_transport', 'get_realization'),
    ),
    _ReplayProbe(
        'numerical_comparison',
        'cad_numerical_comparisons',
        ('comparison_id',),
        _get('numerical_transport', 'get_comparison'),
    ),
    _ReplayProbe(
        'imaging_measurement_chain',
        'cad_imaging_measurement_chains',
        ('chain_id',),
        _get('numerical_transport', 'get_imaging_chain'),
    ),
    _ReplayProbe(
        'camera_calibration',
        'cad_camera_calibrations',
        ('calibration_id',),
        _get('numerical_transport', 'get_camera_calibration'),
    ),
    _ReplayProbe(
        'camera_derived_observation',
        'cad_camera_derived_observations',
        ('observation_id',),
        _get('numerical_transport', 'get_camera_observation'),
    ),
    _ReplayProbe(
        'wireless_av_link',
        'cad_wireless_av_links',
        ('link_id',),
        _get('numerical_transport', 'get_wireless_link'),
    ),
    _ReplayProbe(
        'wireless_transport_observation',
        'cad_wireless_transport_observations',
        ('observation_id',),
        _get('numerical_transport', 'get_wireless_observation'),
    ),
    _ReplayProbe(
        'wireless_sync_evidence',
        'cad_wireless_sync_evidence',
        ('evidence_id',),
        _get('numerical_transport', 'get_wireless_sync'),
    ),
    # REV59-DRAWPROF: #741/#742/#733
    _ReplayProbe(
        'ht_video_design_profile',
        'cad_ht_video_design_profiles',
        ('profile_id',),
        _get('presentation_profile', 'get_video_profile'),
    ),
    _ReplayProbe(
        'ceb23_evaluation',
        'cad_ceb23_evaluations',
        ('evaluation_id',),
        _get('presentation_profile', 'get_ceb23_evaluation'),
    ),
    _ReplayProbe(
        'drawing_symbol_profile',
        'cad_drawing_symbol_profiles',
        ('profile_id',),
        _get('presentation_profile', 'get_symbol_profile'),
    ),
    _ReplayProbe(
        'device_symbol_mapping',
        'cad_device_symbol_mappings',
        ('mapping_id',),
        _get('presentation_profile', 'get_symbol_mapping'),
    ),
    _ReplayProbe(
        'drawing_export_record',
        'cad_drawing_export_records',
        ('export_id',),
        _get('presentation_profile', 'get_drawing_export'),
    ),
    _ReplayProbe(
        'timed_text_profile',
        'cad_timed_text_profiles',
        ('profile_id',),
        _get('presentation_profile', 'get_text_profile'),
    ),
    _ReplayProbe(
        'caption_render_observation',
        'cad_caption_render_observations',
        ('observation_id',),
        _get('presentation_profile', 'get_caption_observation'),
    ),
    # REV59-CODEPOLICY: #746/#748/#722 code-policy authorities
    _ReplayProbe(
        'life_safety_profile',
        'cad_life_safety_profiles',
        ('profile_id',),
        _get('code_policy', 'get_life_safety_profile'),
    ),
    _ReplayProbe(
        'circulation_route',
        'cad_circulation_routes',
        ('route_id',),
        _get('code_policy', 'get_circulation_route'),
    ),
    _ReplayProbe(
        'seating_accessibility_requirement',
        'cad_seating_accessibility_requirements',
        ('requirement_id',),
        _get('code_policy', 'get_accessibility_requirement'),
    ),
    _ReplayProbe(
        'egress_evidence',
        'cad_egress_evidence_records',
        ('evidence_id',),
        _get('code_policy', 'get_egress_evidence'),
    ),
    _ReplayProbe(
        'professional_approval_ref',
        'cad_professional_approval_refs',
        ('approval_id',),
        _get('code_policy', 'get_professional_approval'),
    ),
    _ReplayProbe(
        'dimming_temporal_profile',
        'cad_dimming_temporal_profiles',
        ('profile_id',),
        _get('code_policy', 'get_dimming_profile'),
    ),
    _ReplayProbe(
        'temporal_light_waveform',
        'cad_temporal_light_waveforms',
        ('waveform_id',),
        _get('code_policy', 'get_light_waveform'),
    ),
    _ReplayProbe(
        'lighting_tlm_observation',
        'cad_lighting_tlm_observations',
        ('observation_id',),
        _get('code_policy', 'get_tlm_observation'),
    ),
    _ReplayProbe(
        'lighting_tla_assessment',
        'cad_lighting_tla_assessments',
        ('assessment_id',),
        _get('code_policy', 'get_tla_assessment'),
    ),
    _ReplayProbe(
        'project_data_classification',
        'cad_project_data_classifications',
        ('classification_id',),
        _get('code_policy', 'get_data_classification'),
    ),
    _ReplayProbe(
        'sensitive_artifact_policy',
        'cad_sensitive_artifact_policies',
        ('policy_id',),
        _get('code_policy', 'get_artifact_policy'),
    ),
    _ReplayProbe(
        'export_redaction_manifest',
        'cad_export_redaction_manifests',
        ('manifest_id',),
        _get('code_policy', 'get_export_manifest'),
    ),
    _ReplayProbe(
        'retention_policy_record',
        'cad_retention_policy_records',
        ('record_id',),
        _get('code_policy', 'get_retention_record'),
    ),
    # REV59-ACOUST3: #694/#646/#648 treatment-safety authorities
    _ReplayProbe(
        'treatment_safety',
        'cad_finite_absorber_geometries',
        ('geometry_id',),
        _get('treatment_safety', 'get_finite_geometry'),
    ),
    _ReplayProbe(
        'treatment_safety',
        'cad_finite_treatment_boundary_models',
        ('model_id',),
        _get('treatment_safety', 'get_boundary_model'),
    ),
    _ReplayProbe(
        'treatment_safety',
        'cad_precedence_profiles',
        ('profile_id',),
        _get('treatment_safety', 'get_precedence_profile'),
    ),
    _ReplayProbe(
        'treatment_safety',
        'cad_echo_risk_observations',
        ('observation_id',),
        _get('treatment_safety', 'get_echo_observation'),
    ),
    _ReplayProbe(
        'treatment_safety',
        'cad_reaction_to_fire_evidence',
        ('evidence_id',),
        _get('treatment_safety', 'get_fire_evidence'),
    ),
    _ReplayProbe(
        'treatment_safety',
        'cad_finish_assembly_evidence',
        ('assembly_id',),
        _get('treatment_safety', 'get_assembly_evidence'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_listening_experiment_plans',
        ('plan_id',),
        _get('listening_evidence', 'get_listening_plan'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_listener_qualifications',
        ('qualification_id',),
        _get('listening_evidence', 'get_listener_qualification'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_subjective_inference_records',
        ('record_id',),
        _get('listening_evidence', 'get_inference_record'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_assistive_listening_paths',
        ('path_id',),
        _get('listening_evidence', 'get_als_path'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_als_qualifications',
        ('qualification_id',),
        _get('listening_evidence', 'get_als_qualification'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_receiver_compatibility_evidence',
        ('evidence_id',),
        _get('listening_evidence', 'get_receiver_evidence'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_dynamic_binaural_sessions',
        ('session_id',),
        _get('listening_evidence', 'get_binaural_session'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_pose_tracking_evidence',
        ('evidence_id',),
        _get('listening_evidence', 'get_pose_evidence'),
    ),
    _ReplayProbe(
        'listening_evidence',
        'cad_binaural_qualifications',
        ('qualification_id',),
        _get('listening_evidence', 'get_binaural_qualification'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_receiver_reference_points',
        ('reference_id',),
        _get('acoustic_metrology', 'get_receiver_reference'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_microphone_capsule_poses',
        ('pose_id',),
        _get('acoustic_metrology', 'get_capsule_pose'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_measurement_fixtures',
        ('fixture_id',),
        _get('acoustic_metrology', 'get_fixture'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_fixture_scattering_evidence',
        ('evidence_id',),
        _get('acoustic_metrology', 'get_scattering_evidence'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_discrete_reflection_events',
        ('event_id',),
        _get('acoustic_metrology', 'get_reflection_event'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_echo_diagnostics',
        ('diagnostic_id',),
        _get('acoustic_metrology', 'get_echo_diagnostic'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_drr_method_profiles',
        ('profile_id',),
        _get('acoustic_metrology', 'get_drr_method'),
    ),
    _ReplayProbe(
        'acoustic_metrology',
        'cad_drr_measurements',
        ('measurement_id',),
        _get('acoustic_metrology', 'get_drr_measurement'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_adaptive_identification_profiles',
        ('profile_id',),
        _get('field_metrology', 'get_identification_profile'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_arbitrary_stimulus_measurements',
        ('stimulus_id',),
        _get('field_metrology', 'get_stimulus_measurement'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_adaptive_transfer_estimates',
        ('estimate_id',),
        _get('field_metrology', 'get_transfer_estimate'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_adaptive_residual_evidence',
        ('residual_id',),
        _get('field_metrology', 'get_residual_evidence'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_live_tf_sessions',
        ('session_id',),
        _get('field_metrology', 'get_live_session'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_dual_channel_tf_observations',
        ('observation_id',),
        _get('field_metrology', 'get_tf_observation'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_coherence_observations',
        ('coherence_id',),
        _get('field_metrology', 'get_coherence_observation'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_reference_delay_tracks',
        ('track_id',),
        _get('field_metrology', 'get_delay_track'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_microphone_array_geometries',
        ('geometry_id',),
        _get('field_metrology', 'get_array_geometry'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_spatial_sampling_capabilities',
        ('capability_id',),
        _get('field_metrology', 'get_sampling_capability'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_beamforming_transforms',
        ('transform_id',),
        _get('field_metrology', 'get_beamforming_transform'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_impedance_measurement_profiles',
        ('profile_id',),
        _get('field_metrology', 'get_impedance_profile'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_impedance_calibration_states',
        ('calibration_id',),
        _get('field_metrology', 'get_impedance_calibration'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_measured_load_evidence',
        ('evidence_id',),
        _get('field_metrology', 'get_load_evidence'),
    ),
    _ReplayProbe(
        'field_metrology',
        'cad_thiele_small_derivations',
        ('derivation_id',),
        _get('field_metrology', 'get_ts_derivation'),
    ),
    _ReplayProbe(
        'facility',
        'cad_power_sequence_plans',
        ('plan_id',),
        _get('facility', 'get_power_plan'),
    ),
    _ReplayProbe(
        'facility',
        'cad_power_sequence_evidence',
        ('evidence_id',),
        _get('facility', 'get_power_evidence'),
    ),
    _ReplayProbe(
        'facility',
        'cad_power_quality_observations',
        ('observation_id',),
        _get('facility', 'get_quality_observation'),
    ),
    _ReplayProbe(
        'facility',
        'cad_indoor_air_observations',
        ('observation_id',),
        _get('facility', 'get_indoor_observation'),
    ),
    _ReplayProbe(
        'facility',
        'cad_material_emission_evidence',
        ('evidence_id',),
        _get('facility', 'get_emission_evidence'),
    ),
    _ReplayProbe(
        'facility',
        'cad_product_safety_evidence',
        ('evidence_id',),
        _get('facility', 'get_safety_evidence'),
    ),
    _ReplayProbe(
        'facility',
        'cad_emc_compliance_evidence',
        ('evidence_id',),
        _get('facility', 'get_emc_evidence'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_displayed_gradation_observations',
        ('observation_id',),
        _get('display_authority', 'get_gradation_observation'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_colour_volume_measurements',
        ('volume_id',),
        _get('display_authority', 'get_colour_volume'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_spatial_resolution_evidence',
        ('evidence_id',),
        _get('display_authority', 'get_resolution_evidence'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_low_luminance_capabilities',
        ('capability_id',),
        _get('display_authority', 'get_luminance_capability'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_dynamic_contrast_measurements',
        ('measurement_id',),
        _get('display_authority', 'get_contrast_measurement'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_display_wall_boundaries',
        ('boundary_id',),
        _get('display_authority', 'get_wall_boundary'),
    ),
    _ReplayProbe(
        'display_authority',
        'cad_wall_acoustic_impacts',
        ('impact_id',),
        _get('display_authority', 'get_wall_impact'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_panning_continuity_evidence',
        ('evidence_id',),
        _get('audio_perception', 'get_continuity_evidence'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_subwoofer_localization_profiles',
        ('profile_id',),
        _get('audio_perception', 'get_localization_profile'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_groupdelay_audibility',
        ('verdict_id',),
        _get('audio_perception', 'get_groupdelay_verdict'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_headphone_coupling_evidence',
        ('coupling_id',),
        _get('audio_perception', 'get_coupling_evidence'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_structureborne_paths',
        ('path_id',),
        _get('audio_perception', 'get_structureborne_path'),
    ),
    _ReplayProbe(
        'audio_perception',
        'cad_spatial_remapping_evidence',
        ('evidence_id',),
        _get('audio_perception', 'get_remap_evidence'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_codec_fidelity_evidence',
        ('evidence_id',),
        _get('signal_authority', 'get_codec_evidence'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_fft_spectral_estimator_profiles',
        ('profile_id',),
        _get('signal_authority', 'get_spectral_profile'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_clock_domain_observations',
        ('observation_id',),
        _get('signal_authority', 'get_clock_observation'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_external_fact_claims',
        ('claim_id',),
        _get('signal_authority', 'get_fact_claim'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_fact_conflict_resolutions',
        ('resolution_id',),
        _get('signal_authority', 'get_fact_resolution'),
    ),
    _ReplayProbe(
        'signal_authority',
        'cad_bom_estimates',
        ('estimate_id',),
        _get('signal_authority', 'get_bom_estimate'),
    ),
    _ReplayProbe(
        'cadref',
        'cad_cadence_delivery_evidence',
        ('evidence_id',),
        _get('cadref', 'get_cadence_evidence'),
    ),
    _ReplayProbe(
        'cadref',
        'cad_reference_room_profiles',
        ('profile_id',),
        _get('cadref', 'get_reference_profile'),
    ),
    _ReplayProbe(
        'verauto',
        'cad_verification_requirements',
        ('requirement_id',),
        _get('verauto', 'get_verification_requirement'),
    ),
    _ReplayProbe(
        'verauto',
        'cad_verification_closures',
        ('closure_id',),
        _get('verauto', 'get_verification_closure'),
    ),
    # REV59-LOUDSPK: #754/#734/#731/#732/#737/#735 loudspeaker-output,
    # receiver and install authorities
    _ReplayProbe(
        'large_signal_model',
        'cad_large_signal_models',
        ('model_id',),
        _get('loudspeaker_evidence', 'get_large_signal_model'),
    ),
    _ReplayProbe(
        'excursion_capability',
        'cad_excursion_capabilities',
        ('capability_id',),
        _get('loudspeaker_evidence', 'get_excursion_capability'),
    ),
    _ReplayProbe(
        'vent_flow_capability',
        'cad_vent_flow_capabilities',
        ('capability_id',),
        _get('loudspeaker_evidence', 'get_vent_flow_capability'),
    ),
    _ReplayProbe(
        'mechanical_output_limit',
        'cad_mechanical_output_limits',
        ('assessment_id',),
        _get('loudspeaker_evidence', 'get_output_limit'),
    ),
    _ReplayProbe(
        'source_normalization',
        'cad_source_normalizations',
        ('normalization_id',),
        _get('loudspeaker_evidence', 'get_source_normalization'),
    ),
    _ReplayProbe(
        'reference_drive_condition',
        'cad_reference_drive_conditions',
        ('condition_id',),
        _get('loudspeaker_evidence', 'get_drive_condition'),
    ),
    _ReplayProbe(
        'absolute_output_anchor',
        'cad_absolute_output_anchors',
        ('anchor_id',),
        _get('loudspeaker_evidence', 'get_output_anchor'),
    ),
    _ReplayProbe(
        'sustained_output_test',
        'cad_sustained_output_tests',
        ('test_id',),
        _get('loudspeaker_evidence', 'get_sustained_test'),
    ),
    _ReplayProbe(
        'thermal_compression_observation',
        'cad_thermal_compression_observations',
        ('observation_id',),
        _get('loudspeaker_evidence', 'get_compression_observation'),
    ),
    _ReplayProbe(
        'recovery_profile',
        'cad_recovery_profiles',
        ('profile_id',),
        _get('loudspeaker_evidence', 'get_recovery_profile'),
    ),
    _ReplayProbe(
        'microphone_directional_profile',
        'cad_microphone_directional_profiles',
        ('profile_id',),
        _get('loudspeaker_evidence', 'get_mic_profile'),
    ),
    _ReplayProbe(
        'receiver_orientation_state',
        'cad_receiver_orientation_states',
        ('state_id',),
        _get('loudspeaker_evidence', 'get_orientation_state'),
    ),
    _ReplayProbe(
        'microphone_incidence_applicability',
        'cad_microphone_incidence_applicability',
        ('applicability_id',),
        _get('loudspeaker_evidence', 'get_incidence_applicability'),
    ),
    _ReplayProbe(
        'same_channel_array',
        'cad_same_channel_arrays',
        ('array_id',),
        _get('loudspeaker_evidence', 'get_channel_array'),
    ),
    _ReplayProbe(
        'array_reproduction_mode',
        'cad_array_reproduction_modes',
        ('mode_id',),
        _get('loudspeaker_evidence', 'get_reproduction_mode'),
    ),
    _ReplayProbe(
        'array_acoustic_qualification',
        'cad_array_qualifications',
        ('qualification_id',),
        _get('loudspeaker_evidence', 'get_array_qualification'),
    ),
    _ReplayProbe(
        'loudspeaker_front_layer',
        'cad_loudspeaker_front_layers',
        ('layer_id',),
        _get('loudspeaker_evidence', 'get_front_layer'),
    ),
    _ReplayProbe(
        'grille_transfer_evidence',
        'cad_grille_transfer_evidence',
        ('evidence_id',),
        _get('loudspeaker_evidence', 'get_grille_transfer'),
    ),
    _ReplayProbe(
        'front_layer_applicability',
        'cad_front_layer_applicability',
        ('applicability_id',),
        _get('loudspeaker_evidence', 'get_front_layer_applicability'),
    ),

    # REV59-UNITS: #728 typed physical quantity
    _ReplayProbe(
        'typed_quantity',
        'cad_typed_quantities',
        ('quantity_id',),
        _get('typed_quantity', 'get_quantity'),
    ),
    _ReplayProbe(
        'quantity_operation',
        'cad_quantity_operations',
        ('operation_id',),
        _get('typed_quantity', 'get_operation'),
    ),
    # REV59-UNITS: #730 engineering-assumption ledger
    _ReplayProbe(
        'engineering_assumption',
        'cad_engineering_assumptions',
        ('assumption_id',),
        _get('assumption_ledger', 'get_assumption'),
    ),
    _ReplayProbe(
        'assumption_resolution',
        'cad_assumption_resolutions',
        ('resolution_id',),
        _get('assumption_ledger', 'get_resolution'),
    ),
    _ReplayProbe(
        'permissible_use_assessment',
        'cad_permissible_use_assessments',
        ('assessment_id',),
        _get('assumption_ledger', 'get_assessment'),
    ),
    # REV59-UNITS: #720 perceptual relevance / audibility
    _ReplayProbe(
        'perceptual_model_profile',
        'cad_perceptual_model_profiles',
        ('profile_id',),
        _get('audibility', 'get_profile'),
    ),
    _ReplayProbe(
        'audibility_assessment',
        'cad_audibility_assessments',
        ('assessment_id',),
        _get('audibility', 'get_assessment'),
    ),
    # REV59-UNITS: #719 residual diagnostic-hypothesis
    _ReplayProbe(
        'diagnostic_case',
        'cad_diagnostic_cases',
        ('case_id',),
        _get('diagnostic_hypothesis', 'get_case'),
    ),
    _ReplayProbe(
        'diagnostic_hypothesis',
        'cad_diagnostic_hypotheses',
        ('hypothesis_id',),
        _get('diagnostic_hypothesis', 'get_hypothesis'),
    ),
    _ReplayProbe(
        'diagnostic_test',
        'cad_diagnostic_tests',
        ('test_id',),
        _get('diagnostic_hypothesis', 'get_test'),
    ),
    _ReplayProbe(
        'diagnostic_verdict',
        'cad_diagnostic_verdicts',
        ('verdict_id',),
        _get('diagnostic_hypothesis', 'get_verdict'),
    ),
    # REV59-CLOSEAUX: manifest-gate bridge.
    _ReplayProbe(
        'manifest_gate',
        'cad_manifest_gates',
        ('gate_id',),
        _get('manifest_gates', 'get_manifest_gate'),
    ),
    _ReplayProbe(
        'gate_run_result',
        'cad_gate_run_results',
        ('result_id',),
        _get('manifest_gates', 'get_gate_run_result'),
    ),
    # REV60-EDGE: #779 sub-20 Hz / infrasonic acoustics.
    _ReplayProbe(
        'ulf_profile',
        'cad_ulf_acoustic_profiles',
        ('profile_id',),
        _get('edge', 'get_ulf_profile'),
    ),
    _ReplayProbe(
        'infrasonic_capability',
        'cad_infrasonic_measurement_capabilities',
        ('capability_id',),
        _get('edge', 'get_infrasonic_capability'),
    ),
    _ReplayProbe(
        'ulf_observation',
        'cad_ulf_acoustic_observations',
        ('observation_id',),
        _get('edge', 'get_ulf_observation'),
    ),
    _ReplayProbe(
        'ulf_qualification',
        'cad_ulf_system_qualifications',
        ('qualification_id',),
        _get('edge', 'get_ulf_qualification'),
    ),
    # REV60-EDGE: #781 external noise ingress / façade isolation.
    _ReplayProbe(
        'ingress_scenario',
        'cad_external_noise_ingress_scenarios',
        ('scenario_id',),
        _get('edge', 'get_ingress_scenario'),
    ),
    _ReplayProbe(
        'facade_model',
        'cad_facade_transmission_models',
        ('model_id',),
        _get('edge', 'get_facade_model'),
    ),
    _ReplayProbe(
        'ingress_measurement',
        'cad_external_noise_ingress_measurements',
        ('measurement_id',),
        _get('edge', 'get_ingress_measurement'),
    ),
    _ReplayProbe(
        'ingress_qualification',
        'cad_indoor_noise_ingress_qualifications',
        ('qualification_id',),
        _get('edge', 'get_ingress_qualification'),
    ),
    # REV60-EDGE: #782 material fire-safety evidence.
    _ReplayProbe(
        'fire_safety_profile',
        'cad_fire_safety_evidence_profiles',
        ('profile_id',),
        _get('edge', 'get_fire_safety_profile'),
    ),
    _ReplayProbe(
        'fire_evidence',
        'cad_material_reaction_to_fire_evidence',
        ('evidence_id',),
        _get('edge', 'get_fire_evidence'),
    ),
    _ReplayProbe(
        'material_requirement',
        'cad_installed_material_safety_requirements',
        ('requirement_id',),
        _get('edge', 'get_material_requirement'),
    ),
    _ReplayProbe(
        'fire_safety_approval',
        'cad_fire_safety_approval_refs',
        ('approval_id',),
        _get('edge', 'get_fire_safety_approval'),
    ),
    # REV60-EDGE: #783 accessible media playback.
    _ReplayProbe(
        'accessible_media_profile',
        'cad_accessible_media_profiles',
        ('profile_id',),
        _get('edge', 'get_accessible_media_profile'),
    ),
    _ReplayProbe(
        'caption_observation',
        'cad_caption_presentation_observations',
        ('observation_id',),
        _get('edge', 'get_caption_observation'),
    ),
    _ReplayProbe(
        'ad_observation',
        'cad_audio_description_playback_observations',
        ('observation_id',),
        _get('edge', 'get_ad_observation'),
    ),
    _ReplayProbe(
        'accessible_qualification',
        'cad_accessible_playback_qualifications',
        ('qualification_id',),
        _get('edge', 'get_accessible_qualification'),
    ),

    # REV60-COLLABENV: #721 collaboration/approval authority
    _ReplayProbe(
        'collaboration_actor',
        'cad_collaboration_actors',
        ('actor_id',),
        _get('collaboration', 'get_actor'),
    ),
    _ReplayProbe(
        'revision_authorship',
        'cad_revision_authorship',
        ('authorship_id',),
        _get('collaboration', 'get_authorship'),
    ),
    _ReplayProbe(
        'information_state',
        'cad_information_states',
        ('state_id',),
        _get('collaboration', 'get_information_state'),
    ),
    _ReplayProbe(
        'approval_record',
        'cad_approval_records',
        ('approval_id',),
        _get('collaboration', 'get_approval'),
    ),
    _ReplayProbe(
        'review_decision',
        'cad_review_decisions',
        ('decision_id',),
        _get('collaboration', 'get_review_decision'),
    ),
    _ReplayProbe(
        'sibling_divergence',
        'cad_sibling_divergences',
        ('assessment_id',),
        _get('collaboration', 'get_sibling_divergence'),
    ),
    _ReplayProbe(
        'conflict_resolution',
        'cad_conflict_resolutions',
        ('resolution_id',),
        _get('collaboration', 'get_conflict_resolution'),
    ),
    _ReplayProbe(
        'branch_proposal',
        'cad_branch_proposals',
        ('proposal_id',),
        _get('collaboration', 'get_branch_proposal'),
    ),
    _ReplayProbe(
        'proposal_promotion',
        'cad_proposal_promotions',
        ('promotion_id',),
        _get('collaboration', 'get_proposal_promotion'),
    ),
    _ReplayProbe(
        'client_acceptance',
        'cad_client_acceptances',
        ('acceptance_id',),
        _get('collaboration', 'get_client_acceptance'),
    ),
    _ReplayProbe(
        'collaboration_event',
        'cad_collaboration_events',
        ('event_id',),
        _get('collaboration', 'get_event'),
    ),
    # REV60-COLLABENV: #776 material environmental/aging applicability
    _ReplayProbe(
        'material_condition_state',
        'cad_material_condition_states',
        ('condition_id',),
        _get('material_condition', 'get_condition_state'),
    ),
    _ReplayProbe(
        'material_durability_evidence',
        'cad_material_durability_evidence',
        ('evidence_id',),
        _get('material_condition', 'get_durability_evidence'),
    ),
    _ReplayProbe(
        'material_evidence_applicability',
        'cad_material_evidence_applicability',
        ('applicability_id',),
        _get('material_condition', 'get_applicability'),
    ),
    _ReplayProbe(
        'material_reinspection',
        'cad_material_reinspections',
        ('assessment_id',),
        _get('material_condition', 'get_reinspection'),
    ),
    # #806 calibration deployment/verification loop.
    _ReplayProbe(
        'deployment_capability_declaration',
        'cad_deployment_capability_declarations',
        ('declaration_id',),
        _get('deployment', 'get_capability_declaration'),
    ),
    _ReplayProbe(
        'calibration_deployment',
        'cad_calibration_deployments',
        ('deployment_id',),
        _get('deployment', 'get_deployment'),
    ),
    _ReplayProbe(
        'deployment_effectiveness_report',
        'cad_deployment_effectiveness_reports',
        ('report_id',),
        _get('deployment', 'get_effectiveness_report'),
    ),
    _ReplayProbe(
        'deployment_rollback',
        'cad_deployment_rollbacks',
        ('rollback_id',),
        _get('deployment', 'get_rollback'),
    ),
    # #790 playback-electronics / electrical audio-path authority.
    _ReplayProbe(
        'electronic_audio_path',
        'cad_electronic_audio_path_profiles',
        ('profile_id',),
        _get('playback_electronics', 'get_path_profile'),
    ),
    _ReplayProbe(
        'electrical_transfer',
        'cad_electrical_transfer_measurements',
        ('measurement_id',),
        _get('playback_electronics', 'get_transfer_measurement'),
    ),
    _ReplayProbe(
        'electronic_linearity',
        'cad_electronic_linearity_evidence',
        ('evidence_id',),
        _get('playback_electronics', 'get_linearity_evidence'),
    ),
    _ReplayProbe(
        'playback_electronics_qualification',
        'cad_playback_electronics_qualifications',
        ('qualification_id',),
        _get('playback_electronics', 'get_qualification'),
    ),

    # REV62: #792 lifecycle/supportability authority.
    _ReplayProbe(
        'supportability_profile',
        'cad_supportability_profiles',
        ('profile_id',),
        _get('supportability', 'get_supportability_profile'),
    ),
    _ReplayProbe(
        'external_dependency',
        'cad_supportability_dependencies',
        ('dependency_id',),
        _get('supportability', 'get_external_dependency'),
    ),
    _ReplayProbe(
        'lifecycle_observation',
        'cad_lifecycle_risk_observations',
        ('observation_id',),
        _get('supportability', 'get_lifecycle_observation'),
    ),
    _ReplayProbe(
        'offline_continuity',
        'cad_offline_continuity_evidence',
        ('evidence_id',),
        _get('supportability', 'get_offline_continuity'),
    ),
    _ReplayProbe(
        'replacement_readiness',
        'cad_replacement_readiness',
        ('readiness_id',),
        _get('supportability', 'get_replacement_readiness'),
    ),

    # REV62: #793 single-channel live-observation authority.
    _ReplayProbe(
        'realtime_measurement_session',
        'cad_realtime_measurement_sessions',
        ('session_id',),
        _get('live_spectrum', 'get_session'),
    ),
    _ReplayProbe(
        'live_spectrum_observation',
        'cad_live_spectrum_observations',
        ('observation_id',),
        _get('live_spectrum', 'get_observation'),
    ),
    _ReplayProbe(
        'spl_time_history',
        'cad_spl_time_histories',
        ('history_id',),
        _get('live_spectrum', 'get_history'),
    ),
    _ReplayProbe(
        'captured_live_trace',
        'cad_captured_live_traces',
        ('trace_id',),
        _get('live_spectrum', 'get_trace'),
    ),
    _ReplayProbe(
        'live_event_annotation',
        'cad_live_event_annotations',
        ('annotation_id',),
        _get('live_spectrum', 'get_annotation'),
    ),
    # REV63: #809 benchmark-qualification authority.
    _ReplayProbe(
        'benchmark_scene_mapping',
        'cad_benchmark_scene_mappings',
        ('mapping_id',),
        _get('benchmark_qualification', 'get_mapping'),
    ),
    _ReplayProbe(
        'benchmark_preregistration',
        'cad_benchmark_preregistrations',
        ('preregistration_id',),
        _get('benchmark_qualification', 'get_preregistration'),
    ),
    _ReplayProbe(
        'benchmark_qualification',
        'cad_benchmark_qualifications',
        ('qualification_id',),
        _get('benchmark_qualification', 'get_qualification'),
    ),

    # REV63: #810 uncertainty-aware validation authority.
    _ReplayProbe(
        'validation_uncertainty_protocol',
        'cad_validation_uncertainty_protocols',
        ('protocol_id',),
        _get('validation_uncertainty', 'get_protocol'),
    ),
    _ReplayProbe(
        'observable_uncertainty_evaluation',
        'cad_observable_uncertainty_evaluations',
        ('evaluation_id',),
        _get('validation_uncertainty', 'get_evaluation'),
    ),
    _ReplayProbe(
        'uncertainty_validation_verdict',
        'cad_uncertainty_validation_verdicts',
        ('verdict_id',),
        _get('validation_uncertainty', 'get_verdict'),
    ),

    # REV63: #811 solver-confidence-bound authority.
    _ReplayProbe(
        'material_input_authority',
        'cad_material_input_authorities',
        ('material_id',),
        _get('solver_confidence', 'get_material_authority'),
    ),
    _ReplayProbe(
        'source_directivity_authority',
        'cad_source_directivity_authorities',
        ('directivity_id',),
        _get('solver_confidence', 'get_directivity_authority'),
    ),
    _ReplayProbe(
        'geometry_input_authority',
        'cad_geometry_input_authorities',
        ('geometry_id',),
        _get('solver_confidence', 'get_geometry_authority'),
    ),
    _ReplayProbe(
        'pose_input_authority',
        'cad_pose_input_authorities',
        ('pose_id',),
        _get('solver_confidence', 'get_pose_authority'),
    ),
    _ReplayProbe(
        'solver_input_envelope',
        'cad_solver_input_envelopes',
        ('envelope_id',),
        _get('solver_confidence', 'get_input_envelope'),
    ),
    _ReplayProbe(
        'claim_bound_record',
        'cad_claim_bound_records',
        ('record_id',),
        _get('solver_confidence', 'get_claim_bound'),
    ),

    # REV63: #789 surge/lightning transient-protection authority.
    _ReplayProbe(
        'protected_path',
        'cad_protected_paths',
        ('path_id',),
        _get('transient_protection', 'get_path'),
    ),
    _ReplayProbe(
        'transient_protection_plan',
        'cad_transient_protection_plans',
        ('plan_id',),
        _get('transient_protection', 'get_plan'),
    ),
    _ReplayProbe(
        'spd_evidence',
        'cad_spd_evidence',
        ('spd_id',),
        _get('transient_protection', 'get_spd'),
    ),
    _ReplayProbe(
        'transient_protection_observation',
        'cad_transient_protection_observations',
        ('observation_id',),
        _get('transient_protection', 'get_observation'),
    ),
    _ReplayProbe(
        'transient_protection_event',
        'cad_transient_protection_events',
        ('event_id',),
        _get('transient_protection', 'get_event'),
    ),
    _ReplayProbe(
        'transient_protection_assessment',
        'cad_transient_protection_assessments',
        ('assessment_id',),
        _get('transient_protection', 'get_assessment'),
    ),

    # REV63: #791 operational-energy authority.
    _ReplayProbe(
        'device_power_mode_observation',
        'cad_device_power_mode_observations',
        ('observation_id',),
        _get('operational_energy', 'get_observation'),
    ),
    _ReplayProbe(
        'networked_standby_evidence',
        'cad_networked_standby_evidence',
        ('evidence_id',),
        _get('operational_energy', 'get_standby_evidence'),
    ),
    _ReplayProbe(
        'operational_energy_scenario',
        'cad_operational_energy_scenarios',
        ('scenario_id',),
        _get('operational_energy', 'get_scenario'),
    ),
    _ReplayProbe(
        'energy_use_derivation',
        'cad_energy_use_derivations',
        ('derivation_id',),
        _get('operational_energy', 'get_derivation'),
    ),

    # REV63: #813 owned-room campaign authority.
    _ReplayProbe(
        'campaign_preregistration',
        'cad_campaign_preregistrations',
        ('preregistration_id',),
        _get('owned_room_campaign', 'get_preregistration'),
    ),
    _ReplayProbe(
        'campaign_measurement',
        'cad_campaign_measurements',
        ('measurement_id',),
        _get('owned_room_campaign', 'get_measurement'),
    ),
    _ReplayProbe(
        'campaign_verdict',
        'cad_campaign_verdicts',
        ('verdict_id',),
        _get('owned_room_campaign', 'get_verdict'),
    ),

    # REV63: #801 production-readiness gate.
    _ReplayProbe(
        'production_readiness_decision',
        'cad_production_readiness_decisions',
        ('decision_id',),
        _get('production_readiness', 'get_decision'),
    ),
    _ReplayProbe(
        'recommendation_surface_decision',
        'cad_recommendation_surface_decisions',
        ('surface_decision_id',),
        _get('production_readiness', 'get_surface_decision'),
    ),

    # REV63: #812 hybrid-composition validation authority.
    _ReplayProbe(
        'hybrid_composition_validation_spec',
        'cad_hybrid_composition_validation_specs',
        ('spec_id',),
        _get('hybrid_composition_validation', 'get_spec'),
    ),
    _ReplayProbe(
        'hybrid_validation_evidence',
        'cad_hybrid_validation_evidence',
        ('evidence_id',),
        _get('hybrid_composition_validation', 'get_evidence'),
    ),
    _ReplayProbe(
        'hybrid_validation_verdict',
        'cad_hybrid_validation_verdicts',
        ('verdict_id',),
        _get('hybrid_composition_validation', 'get_verdict'),
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
    # REV48: acceptance-evidence rows are digest-bound managed-asset
    # manifests — each must resolve to its retained file.
    ('htdt_acceptance_evidence', 'sha256', 'size_bytes', 'relative_path', None),

)

_REPLAY_TABLES = frozenset(probe.table for probe in _REPLAY_PROBES)

# REV48 guided acceptance: the audit replays each revision row through the
# repository, which re-derives the chained run_sha256.


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
    'capture_authoring_provenances': (
        'STRUCTURAL_ONLY',
        'capture measurement-promotion provenance bindings; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
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
    # ---- operational bookkeeping -------------------------------------
    'capture_inbox_items': (
        'OPERATIONAL_METADATA',
        'capture inbox triage/disposition bookkeeping — operational '
        'intake state, not canonical authority',
    ),
    'capture_disposition_transitions': (
        'OPERATIONAL_METADATA',
        'capture inbox disposition audit ledger — append-only '
        'operational bookkeeping',
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
    'cad_auralization_artifacts': (
        'STRUCTURAL_ONLY',
        'auralization artifact authority; canonical replay path pending '
        '— strongest verification is schema + payload parse',
    ),
    'cad_auralization_render_specs': (
        'STRUCTURAL_ONLY',
        'auralization render spec authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_auralization_capabilities': (
        'STRUCTURAL_ONLY',
        'auralization capability authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_auralization_listening_validations': (
        'STRUCTURAL_ONLY',
        'measured-vs-predicted listening validation authority; canonical '
        'replay path pending — strongest verification is schema + '
        'payload parse',
    ),
    'cad_auralization_review_packages': (
        'STRUCTURAL_ONLY',
        'auralization review package manifest authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'cad_auralization_routing_declarations': (
        'STRUCTURAL_ONLY',
        'auralization routing declaration authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
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
    'cad_late_decay_estimate_artifacts': (
        'STRUCTURAL_ONLY',
        'late-decay estimate artifact authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
    ),
    'cad_late_field_artifacts': (
        'STRUCTURAL_ONLY',
        'late-field energy artifact authority; canonical replay path '
        'pending — strongest verification is schema + payload parse',
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
    'field_return_applications': (
        'STRUCTURAL_ONLY',
        'field return application ledger authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
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
    'r160_stitched_hybrid_responses': (
        'STRUCTURAL_ONLY',
        'stitched union-band hybrid response authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'r160_late_energy_decay_artifacts': (
        'STRUCTURAL_ONLY',
        'bounded late-energy decay artifact authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
    ),
    'cad_acoustic_geometry_derivations': (
        'STRUCTURAL_ONLY',
        'acoustic geometry derivation authority; canonical replay path '
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
    'cad_solver_capability_manifests': (
        'STRUCTURAL_ONLY',
        'solver capability manifest authority; canonical replay path '
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
    'cad_stochastic_receiver_estimate_artifacts': (
        'STRUCTURAL_ONLY',
        'stochastic receiver estimate artifact authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
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
    'capture_mission_rebase_decisions': (
        'STRUCTURAL_ONLY',
        'mission rebase decision ledger authority; canonical replay '
        'path pending — strongest verification is schema + payload parse',
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
    'cad_active_lf_control_events': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_active_lf_control_plans': (
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
    'cad_video_commissioning_sessions': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_commissioning_status_events': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_readiness_reports': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_diagnoses': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_action_proposals': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_operator_adjustments': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_before_after_comparisons': (
        'STRUCTURAL_ONLY',
        'structural payload integrity — no dedicated canonical replay adapter registered for this family',
    ),
    'cad_video_import_batches': (
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
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> AuthorityAuditReport:
    """Replay every persisted authority in ``database_path`` fail-closed.

    ``database_path`` is the SQLite file; its parent directory must carry
    the managed-asset subtree (``measurement-assets/``). Repository
    construction may initialize or migrate the schema, so callers always
    audit a clone — never a file whose bytes are pinned by a manifest.

    Each row is classified and reported rather than aborting at the first
    failure so operators see the full damage surface. ``is_cancelled`` is a
    cooperative-cancel poll evaluated per row — a full replay can far
    outlive a worker shutdown budget, so backup callers pass their cancel
    flag through to abort mid-pass instead of being detached mid-verify.
    """

    def _poll_cancel() -> None:
        if is_cancelled is None:
            return
        # Lazy: native_backup imports this module inside its own functions,
        # so a module-level import here would be circular.
        from .native_backup import _raise_if_backup_cancelled

        _raise_if_backup_cancelled(is_cancelled)

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

    connection = connect_sqlite(db_path)
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
                _poll_cancel()
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
                _poll_cancel()
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
                _poll_cancel()
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
                _poll_cancel()
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
                    _poll_cancel()
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
        chain.close()
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
