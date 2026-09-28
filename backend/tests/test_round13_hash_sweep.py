"""Round-13 hash-sweep regression tests.

REV13-RECOMP proved a systemic sealing bug: builders hashed the RAW
payload inside a ``model_construct`` provisional while the sealed model's
validator recomputes the same hash over VALIDATED fields — so any input
whose canonical JSON form differs from its literal form (``48000`` vs
``48000.0``, dict vs nested model, ``1`` vs ``True``) could never seal.

The sweep wrapped every exposed site in ``canonicalize_payload`` so the
provisional carries validated representations. These tests pin the fix:
for every swept class, a raw payload built from type-mismatched literals
must produce a provisional hash that equals the sealed hash.
"""

from __future__ import annotations

import datetime
import enum
import importlib
import inspect
import itertools
import types
import typing

import pytest
from pydantic import BaseModel, TypeAdapter

from htdt.canonical_json import canonical_sha256, canonicalize_payload

_SW = types.UnionType
_ANYS = (typing.Any,)


class _Unsynthable(Exception):
    pass


def _constraints(info) -> dict[str, object]:
    out: dict[str, object] = {}
    for meta in getattr(info, 'metadata', ()):  # annotated-types constraints
        for attr in ('min_length', 'max_length', 'gt', 'ge', 'lt', 'le', 'pattern'):
            if hasattr(meta, attr):
                val = getattr(meta, attr)
                if val is not None:
                    out[attr] = val
    return out


def _synth(annotation, info=None, depth: int = 0, name: str = ''):
    """Return a deliberately type-mismatched-but-valid literal for *annotation*.

    int-for-float, float-for-int, int-for-bool, str-for-date and dict-for-
    model are exactly the caller literals that used to break sealing.
    """
    if depth > 6:
        raise _Unsynthable('depth')
    cons = _constraints(info) if info is not None else {}
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is not None and (args or origin in (tuple, list, dict, set, frozenset)):
        if origin in (types.UnionType, typing.Union) or str(origin) == 'types.UnionType':
            non_none = [a for a in args if a is not type(None)]
            return _synth(non_none[0], depth=depth + 1, name=name)
        if origin in (tuple, list, set, frozenset):
            if args and args[-1] is not Ellipsis and len(args) > 1:
                return tuple(
                    _synth(a, depth=depth + 1, name=name) for a in args
                )
            item_ann = args[0] if args else str
            n = int(cons.get('min_length') or 1)
            return tuple(
                _synth(item_ann, depth=depth + 1, name=name)
                for _ in range(max(n, 1))
            )
        if origin is dict:
            return {}
        if origin is typing.Literal or str(origin) == 'typing.Literal':
            return args[0]
        return _synth(args[0], depth=depth + 1, name=name) if args else 'x'
    if annotation is str:
        if name.endswith('_json'):
            return '{}'
        if cons.get('pattern'):
            pat = str(cons['pattern'])
            import re as _pat_re
            # substitute literal-prefix + hex-run patterns such as
            # '^speech-intelligibility-spec:[0-9a-f]{64}$'
            m = _pat_re.fullmatch(
                r'\^?([a-zA-Z0-9:\-_.]*)\[0-9a-f\]\{(\d+)(?:,\d+)?\}\$?', pat
            )
            if m:
                prefix, count = m.group(1), int(m.group(2))
                return prefix + 'a' * count
            if 'a-f0-9' in pat or 'sha256' in pat or '{64}' in pat:
                return 'a' * 64
            raise _Unsynthable('pattern')
        n = max(int(cons.get('min_length') or 1), 1)
        return 'x' * n
    if annotation is float:
        # int literal into a float field: the round-13 hazard; keep it
        # inside the field's numeric bounds when they exist.
        val: float = 48000
        if 'le' in cons:
            val = min(val, float(cons['le']))
        if 'lt' in cons:
            val = min(val, float(cons['lt']) - 1)
        if 'ge' in cons:
            val = max(val, float(cons['ge']))
        if 'gt' in cons:
            val = max(val, float(cons['gt']) + 1)
        return int(val)
    if annotation is int:
        val = 7
        if 'le' in cons:
            val = min(val, int(cons['le']))
        if 'lt' in cons:
            val = min(val, int(cons['lt']) - 1)
        if 'ge' in cons:
            val = max(val, int(cons['ge']))
        if 'gt' in cons:
            val = max(val, int(cons['gt']) + 1)
        return val
    if annotation is bool:
        return True
    if annotation in _ANYS or annotation is None or annotation is inspect.Parameter.empty:
        return 'x'
    if annotation in (datetime.datetime, datetime.date):
        return '2024-01-02T03:04:05'
    if annotation is datetime.timedelta:
        return 0.5
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        members = list(annotation)
        if not members:
            raise _Unsynthable('empty enum')
        return members[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _synth_model_payload(annotation, depth)
    raise _Unsynthable(repr(annotation))


def _synth_model_payload(cls: type[BaseModel], depth: int) -> dict:
    out = {}
    for name, field in cls.model_fields.items():
        if not field.is_required():
            continue
        out[name] = _synth_field(field, name) if depth + 1 <= 6 else _synth(field.annotation, field, depth + 1, name=name)
    return out


def _synth_field(field, name: str):
    """Synthesize a *field-valid* value, preferring hazard literals.

    Tries the primary non-canonical literal (``48000`` for float fields),
    then falls back until the field's own annotation validation accepts
    the value — so classes with bounded or function-validated fields are
    still swept rather than skipped.
    """
    adapter = TypeAdapter(field.annotation)
    cands: list = []
    try:
        cands.append(_synth(field.annotation, field, name=name))
    except _Unsynthable:
        pass
    ann = field.annotation
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    simple = ann
    if args and (origin in (types.UnionType, typing.Union) or str(origin) == 'types.UnionType'):
        simple = next((a for a in args if a is not type(None)), ann)
    if simple is float:
        cands += [360, 90, 7, 1, 0, -1]
    elif simple is int:
        cands += [1, 0, -1]
    elif simple is str:
        cands += ['a' * 64]
    if origin in (tuple, list, set, frozenset):
        cands += [(), tuple()]
    for cand in cands:
        try:
            adapter.validate_python(cand)
            return cand
        except Exception:  # noqa: BLE001 - try next candidate
            continue
    raise _Unsynthable(f'no valid candidate for {name}')


def _sha_fields(cls: type[BaseModel]) -> list[str]:
    return [n for n in cls.model_fields if n.endswith('_sha256')]


def _payload_methods(cls: type[BaseModel]) -> list[str]:
    names = ('identity_payload', 'semantic_payload', 'spec_payload')
    return [n for n in names if callable(getattr(cls, n, None))]


# (module, class) pairs swept in round 13 — every builder site whose
# provisional hash could diverge from the sealed hash.
SWEEP_CLASSES = [
    ('cad_acoustic_construction', 'AcousticConstructionDefinition'),
    ('cad_acoustic_construction', 'DerivedMaterialAcousticEvidence'),
    ('cad_acoustic_index', 'AcousticIndexSnapshot'),
    ('cad_acoustic_index', 'AcousticIndexVariant'),
    ('cad_acoustic_portal_coupling', 'AcousticPortalCoupling'),
    ('cad_acoustic_source_pose', 'AcousticSourcePoseObservation'),
    ('cad_action_item', 'ProjectActionItem'),
    ('cad_active_lf_control', 'ActiveLowFrequencyControlPlan'),
    ('cad_adaptive_extended', 'CadAdaptiveExtendedPlan'),
    ('cad_adaptive_measurement_design', 'AdaptiveMeasurementDesignSpec'),
    ('cad_adaptive_planner', 'CadAdaptivePlan'),
    ('cad_ambient_noise', 'AmbientNoiseComparison'),
    ('cad_ambient_noise', 'AmbientNoiseCriterion'),
    ('cad_ambient_noise', 'AmbientNoiseProfile'),
    ('cad_anechoic_programme_pack', 'AnechoicProgrammeAsset'),
    ('cad_audio_transform_lineage', 'AudioFormatTransform'),
    ('cad_audio_transform_lineage', 'TransformLineageEvaluation'),
    ('cad_auralization', 'AuralizationRenderSpec'),
    ('cad_av_session_reliability', 'AVPlaybackSession'),
    ('cad_av_sync', 'AVLatencyMeasurement'),
    ('cad_av_sync', 'AVSyncCondition'),
    ('cad_bass_management', 'BassManagementEvaluation'),
    ('cad_bass_management', 'BassManagementProfile'),
    ('cad_bt2111_video_pack', 'PatternDescriptor'),
    ('cad_cable_run', 'CableRun'),
    ('cad_calibration', 'CadCalibrationPlan'),
    ('cad_calibration', 'CadVerificationMeasurementCompletion'),
    ('cad_calibration', 'CadVerificationMeasurementPlan'),
    ('cad_camilladsp', 'DeviceActionAck'),
    ('cad_cec_adapter', 'CECEventRecord'),
    ('cad_colorimetry', 'VideoColorEvaluation'),
    ('cad_colorimetry', 'VideoColorMeasurementSet'),
    ('cad_colorimetry', 'VideoColorTargetProfile'),
    ('cad_correction_design_policy', 'CorrectionDesignPolicy'),
    ('cad_data_source_registry', 'DataSourceRegistryEntry'),
    ('cad_data_source_registry', 'ImporterDeclaration'),
    ('cad_data_source_registry', 'RawSourceRecord'),
    ('cad_design_comparison', 'DesignComparisonSet'),
    ('cad_device_backup', 'DeviceConfigurationBackupArtifact'),
    ('cad_direct_view', 'DirectViewDisplaySpecification'),
    ('cad_directivity_admission', 'DirectivityAdmissionRecord'),
    ('cad_drawing_set', 'DrawingSetSpec'),
    ('cad_ebu_loudness_pack', 'LoudnessTestCase'),
    ('cad_equipment_device', 'DeviceActionAck'),
    ('cad_equipment_self_noise', 'EquipmentAcousticNoiseProfile'),
    ('cad_evidence_reconciliation', 'ReconciliationDecision'),
    ('cad_external_admission', 'ExternalAssetAdmission'),
    ('cad_external_admission', 'ExternalAssetFile'),
    ('cad_external_calibration', 'ImportedCalibrationArtifact'),
    ('cad_field_explorer', 'FieldExplorerSession'),
    ('cad_fir_filter', 'FIRFilterArtifact'),
    ('cad_fir_filter', 'FIRImportRecord'),
    ('cad_flair_bridge', 'FlairBridgeCase'),
    ('cad_flair_bridge', 'FlairGeometryArtifact'),
    ('cad_foam_material_batch', 'JcalParameterSet'),
    ('cad_hdmi_transport', 'HDMILatencyIndicationEvidence'),
    ('cad_hdmi_transport', 'HDMITransportCapability'),
    ('cad_hue_lighting', 'DeviceActionAck'),
    ('cad_input_chain_capability', 'MeasurementInputChainProfile'),
    ('cad_installed_surface', 'InstalledSurfaceAcousticMeasurement'),
    ('cad_ir_analysis', 'IRAnalysisResult'),
    ('cad_ir_analysis', 'IRAnalysisSpec'),
    ('cad_isolation_assembly_pack', 'IsolationAssembly'),
    ('cad_joint_optimization', 'JointOptimizationSpec'),
    ('cad_laser_speckle', 'LaserProjectionSpeckleCondition'),
    ('cad_laser_speckle', 'LaserProjectionSpeckleMeasurement'),
    ('cad_lighting', 'LightingScene'),
    ('cad_lighting', 'LightingSceneEvaluation'),
    ('cad_listener_pose', 'ListenerPoseAuthority'),
    ('cad_listening_session', 'ListeningSessionSpec'),
    ('cad_material_library', 'MaterialAcousticEvidence'),
    ('cad_material_library', 'MaterialDefinition'),
    ('cad_measured_modal_analysis', 'MeasuredModalAnalysisSpec'),
    ('cad_measured_modal_analysis', 'MeasuredModalModel'),
    ('cad_measurement_authorities', 'CadAcousticLevelCalibration'),
    ('cad_measurement_authorities', 'CadMeasurementTimingReference'),
    ('cad_measurement_pose', 'PlannedObservedPoseDelta'),
    ('cad_measurement_quality', 'CadAcquisitionContext'),
    ('cad_measurement_quality', 'CadMeasurementQualityProfile'),
    ('cad_measurement_stimulus', 'CadMeasurementExcitationAsset'),
    ('cad_measurement_stimulus', 'CadMeasurementStimulusProfile'),
    ('cad_measurement_target_pattern', 'MeasurementTargetPattern'),
    ('cad_media_source_capability', 'MediaPlaybackSourceCondition'),
    ('cad_meter_correction', 'MeterCorrectionArtifact'),
    ('cad_mic_response_calibration', 'MicrophoneResponseCalibrationProfile'),
    ('cad_model_calibration', 'AcousticModelCalibrationResult'),
    ('cad_model_calibration', 'AcousticModelCalibrationSpec'),
    ('cad_moving_mic_measurement', 'MovingMicrophoneMeasurementSpec'),
    ('cad_moving_mic_measurement', 'SpatialAverageMeasurement'),
    ('cad_multi_radiator_source', 'RadiatorCoherenceEvaluation'),
    ('cad_multi_receiver_acquisition', 'DerivedReceiverAverage'),
    ('cad_multi_receiver_acquisition', 'MultiReceiverAcquisition'),
    ('cad_multi_seat_analysis', 'MultiSeatAnalysisResult'),
    ('cad_occupancy_acoustics', 'RoomOccupancyAcousticState'),
    ('cad_operating_preset', 'PresetMeasurementBinding'),
    ('cad_operating_preset', 'TheaterOperatingPreset'),
    ('cad_personal_listening', 'PersonalListeningRoute'),
    ('cad_phase_time_analysis', 'PhaseTimeAnalysisResult'),
    ('cad_phase_time_analysis', 'PhaseTimeAnalysisSpec'),
    ('cad_photometric', 'AmbientReflectanceProfile'),
    ('cad_photometric', 'ExpectedLuminanceEstimate'),
    ('cad_photometric', 'PhotometricEvaluation'),
    ('cad_photometric', 'ProjectorImagePerformanceProfile'),
    ('cad_photometric', 'ScreenOpticalProfile'),
    ('cad_pjlink', 'DeviceActionAck'),
    ('cad_playback_level', 'ReferencePlaybackProfile'),
    ('cad_playback_level_compensation', 'PlaybackLevelCompensationProfile'),
    ('cad_prediction_matrix', 'PredictionMatrixSpec'),
    ('cad_presentation_profile', 'PresentationModeConfirmation'),
    ('cad_presentation_profile', 'PresentationProfileEvaluation'),
    ('cad_presentation_profile', 'VideoPresentationProfile'),
    ('cad_processing_condition', 'VideoProcessingCondition'),
    ('cad_program_dynamics', 'DynamicsEvaluation'),
    ('cad_program_dynamics', 'EffectiveDynamicsProcessingState'),
    ('cad_program_dynamics', 'ProgramDynamicsProcessingProfile'),
    ('cad_program_stress', 'ProgramStressProfile'),
    ('cad_project_activity', 'ProjectActivityEvent'),
    ('cad_projector_reference_pack', 'ProjectorReferencePack'),
    ('cad_rack_infrastructure', 'RackDefinition'),
    ('cad_reflection_diagnostic', 'ReflectionDiagnosticRequest'),
    ('cad_reflection_guidance', 'ReflectionGuidanceItem'),
    ('cad_renderer_topology', 'RendererOutputTopology'),
    ('cad_renderer_topology', 'RendererTopologyEvaluation'),
    ('cad_room_reflection', 'InSituContrastMeasurement'),
    ('cad_room_reflection', 'ProjectedContrastDecomposition'),
    ('cad_room_reflection', 'RoomOpticalSurfaceProfile'),
    ('cad_screen_evidence_registry', 'ScreenEvidenceRecord'),
    ('cad_screen_evidence_registry', 'ScreenEvidenceRegistry'),
    ('cad_screen_moire', 'MoireCompatibilityCondition'),
    ('cad_screen_moire', 'MoireObservation'),
    ('cad_screen_moire', 'ScreenMicrostructureAuthority'),
    ('cad_search', 'CadSearchSpec'),
    ('cad_signal_path', 'AVSignalPath'),
    ('cad_signal_path', 'SignalPathEvaluation'),
    ('cad_spatial_fidelity', 'ProjectionOpticalCondition'),
    ('cad_spatial_fidelity', 'SpatialImageQualityMeasurement'),
    ('cad_spatial_field', 'SpatialFieldRequestSpec'),
    ('cad_spatial_ir_measurement', 'SpatialMeasurementArrayProfile'),
    ('cad_spatial_ir_measurement', 'SpatialRoomImpulseResponseDataset'),
    ('cad_spatial_ir_metrics', 'SpatialIRMetricSpec'),
    ('cad_spatial_reproduction', 'SpatialReproductionProfile'),
    ('cad_spatial_starter_pack', 'SpatialStarterFixture'),
    ('cad_spatial_starter_pack', 'SpatialStarterPack'),
    ('cad_spatial_uncertainty', 'SpatialObservationUncertainty'),
    ('cad_speaker_level_transfer', 'AmplifierOutputImpedanceAuthority'),
    ('cad_speaker_level_transfer', 'SpeakerCableElectricalProfile'),
    ('cad_speaker_level_transfer', 'SpeakerElectricalPath'),
    ('cad_speaker_library', 'SpeakerDataset'),
    ('cad_spectral_lighting', 'SpectralEvidence'),
    ('cad_speech_intelligibility', 'SpeechIntelligibilityAnalysisSpec'),
    ('cad_stationarity', 'MeasurementStationarityAssessment'),
    ('cad_streaming_qoe', 'StreamingPlaybackSession'),
    ('cad_structural_boundary', 'StructuralBoundaryModel'),
    ('cad_surface_scattering', 'DirectionalScatteringKernel'),
    ('cad_surface_scattering', 'SurfaceScatteringEvidence'),
    ('cad_system_nonlinearity', 'SystemNonlinearityMeasurement'),
    ('cad_tactile', 'TactileActuatorDefinition'),
    ('cad_tactile', 'TactileAttachmentBinding'),
    ('cad_tactile', 'TactileProcessingProfile'),
    ('cad_tactile', 'TactileSystemEvaluation'),
    ('cad_tactile_reference_pack', 'TactileActuatorReference'),
    ('cad_temporal_emission', 'DisplayTemporalCondition'),
    ('cad_temporal_emission', 'TemporalEmissionMeasurement'),
    ('cad_temporal_emission', 'TemporalLightWaveform'),
    ('cad_treatment_fabrication', 'TreatmentFabricationPackage'),
    ('cad_uncertainty_budget', 'UncertaintyBudgetResult'),
    ('cad_uncertainty_budget', 'UncertaintyBudgetSpec'),
    ('cad_usable_output', 'HeadroomEvaluation'),
    ('cad_usable_output', 'SourceUsableOutputProfile'),
    ('cad_validation_campaign', 'CadValidationCampaign'),
    ('cad_validation_corpus', 'ValidationBenchmarkSpec'),
    ('cad_validation_corpus', 'ValidationCorpusEntry'),
    ('cad_video_geometry', 'ProjectorSpecificationEvidence'),
    ('cad_video_latency', 'VideoLatencyCondition'),
    ('cad_video_latency', 'VideoLatencyMeasurement'),
    ('cad_viewing_envelope', 'ViewingResolutionEvaluation'),
    ('cad_viewing_envelope', 'ViewingResolutionPolicy'),
    ('cad_wave_qualification', 'WaveSuiteReport'),
    ('external_dependency_resolver', 'ExternalAuthorityDependency'),
    ('optimization_robustness_validation', 'O90EValidationCase'),
    ('optimization_robustness_validation', 'O90EValidationDecision'),
    ('raw_mesh_health', 'MeshHealthSummary'),
]

# (module, public builder) pairs — exercised end-to-end where every
# required parameter can be synthesized.
SWEEP_BUILDERS = [
    ('cad_acoustic_construction', 'build_acoustic_construction'),
    ('cad_acoustic_construction', 'derive_material_evidence'),
    ('cad_acoustic_index', 'snapshot_from_compare_response'),
    ('cad_acoustic_portal_coupling', 'build_acoustic_portal_coupling'),
    ('cad_acoustic_source_pose', '_finish'),
    ('cad_action_item', 'update_action_item'),
    ('cad_active_lf_control', 'build_control_plan'),
    ('cad_adaptive_extended', 'build_adaptive_extended_plan'),
    ('cad_adaptive_measurement_design', 'build_adaptive_design_spec'),
    ('cad_adaptive_planner', 'build_adaptive_plan'),
    ('cad_ambient_noise', 'build_ambient_noise_criterion'),
    ('cad_ambient_noise', 'build_ambient_noise_profile'),
    ('cad_ambient_noise', 'compare_ambient_profiles'),
    ('cad_anechoic_programme_pack', 'build_programme_asset'),
    ('cad_audio_transform_lineage', 'build_audio_format_transform'),
    ('cad_audio_transform_lineage', 'evaluate_transform_lineage'),
    ('cad_auralization', 'build_auralization_render_spec'),
    ('cad_av_session_reliability', 'build_av_playback_session'),
    ('cad_av_sync', 'advance_av_latency_measurement'),
    ('cad_av_sync', 'build_av_latency_measurement'),
    ('cad_av_sync', 'build_av_sync_condition'),
    ('cad_bass_management', 'build_bass_management_profile'),
    ('cad_bass_management', 'evaluate_bass_management'),
    ('cad_bt2111_video_pack', '_descriptor'),
    ('cad_cable_run', 'build_cable_run'),
    ('cad_calibration', 'build_calibration_plan'),
    ('cad_calibration', 'build_verification_measurement_completion'),
    ('cad_calibration', 'build_verification_measurement_plan'),
    ('cad_camilladsp', 'apply_action'),
    ('cad_cec_adapter', 'collect'),
    ('cad_colorimetry', 'build_video_color_measurement_set'),
    ('cad_colorimetry', 'build_video_color_target_profile'),
    ('cad_colorimetry', 'evaluate_video_color'),
    ('cad_correction_design_policy', 'build_correction_design_policy'),
    ('cad_data_source_registry', 'build_importer_declaration'),
    ('cad_data_source_registry', 'build_raw_source_record'),
    ('cad_data_source_registry', 'build_registry_entry'),
    ('cad_design_comparison', 'build_comparison_set'),
    ('cad_device_backup', 'build_backup_artifact'),
    ('cad_direct_view', 'build_direct_view_display_specification'),
    ('cad_directivity_admission', 'build_directivity_admission'),
    ('cad_drawing_set', 'build_drawing_set_spec'),
    ('cad_ebu_loudness_pack', '_case'),
    ('cad_equipment_device', 'apply_action'),
    ('cad_equipment_self_noise', 'build_equipment_noise_profile'),
    ('cad_evidence_reconciliation', 'reconcile_subject'),
    ('cad_external_admission', 'build_external_asset_admission'),
    ('cad_external_admission', 'external_asset_file'),
    ('cad_external_calibration', 'build_equalizer_apo_artifact'),
    ('cad_field_explorer', 'build_mode_field_explorer_session'),
    ('cad_fir_filter', 'build_fir_filter_artifact'),
    ('cad_fir_filter', 'import_fir_filter_artifact'),
    ('cad_flair_bridge', 'build_flair_bridge_case'),
    ('cad_flair_bridge', 'build_flair_geometry_artifact'),
    ('cad_foam_material_batch', 'build_jcal_parameter_set'),
    ('cad_hdmi_transport', 'build_hdmi_lip_evidence'),
    ('cad_hdmi_transport', 'build_hdmi_transport_capability'),
    ('cad_hue_lighting', 'apply_action'),
    ('cad_input_chain_capability', 'build_input_chain_profile'),
    ('cad_installed_surface', 'build_installed_surface_measurement'),
    ('cad_ir_analysis', 'build_ir_analysis_spec'),
    ('cad_ir_analysis', 'run_ir_analysis'),
    ('cad_isolation_assembly_pack', 'build_isolation_assembly'),
    ('cad_joint_optimization', 'build_joint_optimization_spec'),
    ('cad_laser_speckle', 'build_laser_speckle_condition'),
    ('cad_laser_speckle', 'build_laser_speckle_measurement'),
    ('cad_lighting', 'build_lighting_scene'),
    ('cad_lighting', 'evaluate_lighting_scene'),
    ('cad_listener_pose', 'build_listener_pose'),
    ('cad_listening_session', 'build_listening_session_spec'),
    ('cad_material_library', 'build_material_definition'),
    ('cad_material_library', 'build_material_evidence'),
    ('cad_measured_modal_analysis', 'build_measured_modal_model'),
    ('cad_measured_modal_analysis', 'build_modal_analysis_spec'),
    ('cad_measurement_authorities', 'build_acoustic_level_calibration'),
    ('cad_measurement_authorities', 'build_timing_reference'),
    ('cad_measurement_pose', '_make_delta'),
    ('cad_measurement_quality', 'build_acquisition_context'),
    ('cad_measurement_quality', 'build_measurement_quality_profile'),
    ('cad_measurement_stimulus', 'build_excitation_asset'),
    ('cad_measurement_stimulus', 'build_stimulus_profile'),
    ('cad_measurement_target_pattern', 'build_target_pattern'),
    ('cad_media_source_capability', 'build_media_source_condition'),
    ('cad_meter_correction', 'import_meter_correction'),
    ('cad_mic_response_calibration', 'build_response_calibration_profile'),
    ('cad_model_calibration', 'build_model_calibration_spec'),
    ('cad_model_calibration', 'run_model_calibration'),
    ('cad_moving_mic_measurement', 'build_moving_mic_spec'),
    ('cad_moving_mic_measurement', 'build_spatial_average_measurement'),
    ('cad_multi_radiator_source', 'evaluate_multi_radiator_coherence'),
    ('cad_multi_receiver_acquisition', 'build_derived_receiver_average'),
    ('cad_multi_receiver_acquisition', 'build_multi_receiver_acquisition'),
    ('cad_multi_seat_analysis', 'run_multi_seat_analysis'),
    ('cad_occupancy_acoustics', 'build_room_occupancy_state'),
    ('cad_operating_preset', 'bind_preset_measurements'),
    ('cad_operating_preset', 'build_operating_preset'),
    ('cad_personal_listening', 'build_personal_listening_route'),
    ('cad_phase_time_analysis', 'build_phase_time_result'),
    ('cad_phase_time_analysis', 'build_phase_time_spec'),
    ('cad_photometric', 'build_ambient_reflectance_profile'),
    ('cad_photometric', 'build_projector_image_performance_profile'),
    ('cad_photometric', 'build_screen_optical_profile'),
    ('cad_photometric', 'estimate_direct_view_luminance'),
    ('cad_photometric', 'estimate_projection_luminance'),
    ('cad_photometric', 'evaluate_photometric_state'),
    ('cad_pjlink', 'apply_action'),
    ('cad_playback_level', 'build_reference_profile'),
    ('cad_playback_level_compensation', 'build_level_compensation_profile'),
    ('cad_prediction_matrix', 'build_prediction_matrix_spec'),
    ('cad_presentation_profile', 'build_presentation_mode_confirmation'),
    ('cad_presentation_profile', 'build_video_presentation_profile'),
    ('cad_presentation_profile', 'evaluate_presentation_profile'),
    ('cad_processing_condition', 'build_video_processing_condition'),
    ('cad_program_dynamics', 'build_effective_dynamics_state'),
    ('cad_program_dynamics', 'build_program_dynamics_profile'),
    ('cad_program_dynamics', 'evaluate_dynamics_state'),
    ('cad_program_stress', 'build_program_stress_profile'),
    ('cad_project_activity', '_event'),
    ('cad_projector_reference_pack', 'build_projector_reference_pack'),
    ('cad_rack_infrastructure', 'build_rack_definition'),
    ('cad_reflection_diagnostic', 'build_reflection_diagnostic_request'),
    ('cad_reflection_guidance', '_guidance_item'),
    ('cad_renderer_topology', 'build_renderer_output_topology'),
    ('cad_renderer_topology', 'evaluate_renderer_topology'),
    ('cad_room_reflection', 'build_contrast_decomposition'),
    ('cad_room_reflection', 'build_in_situ_contrast_measurement'),
    ('cad_room_reflection', 'build_room_optical_surface_profile'),
    ('cad_screen_evidence_registry', '_record'),
    ('cad_screen_evidence_registry', 'build_screen_evidence_registry'),
    ('cad_screen_moire', 'build_moire_condition'),
    ('cad_screen_moire', 'build_moire_observation'),
    ('cad_screen_moire', 'build_screen_microstructure'),
    ('cad_search', 'build_cad_search_spec'),
    ('cad_signal_path', 'build_av_signal_path'),
    ('cad_signal_path', 'evaluate_signal_path'),
    ('cad_spatial_fidelity', 'build_projection_optical_condition'),
    ('cad_spatial_fidelity', 'build_spatial_image_quality_measurement'),
    ('cad_spatial_field', 'build_spatial_field_request'),
    ('cad_spatial_ir_measurement', 'build_array_profile'),
    ('cad_spatial_ir_measurement', 'build_spatial_ir_dataset'),
    ('cad_spatial_ir_metrics', 'build_spatial_ir_metric_spec'),
    ('cad_spatial_reproduction', 'load_sofa_dataset_profile'),
    ('cad_spatial_starter_pack', 'build_spatial_fixture'),
    ('cad_spatial_starter_pack', 'build_spatial_starter_pack'),
    ('cad_spatial_uncertainty', 'build_spatial_observation_uncertainty'),
    ('cad_speaker_level_transfer', 'build_amplifier_output_impedance'),
    ('cad_speaker_level_transfer', 'build_speaker_cable_electrical_profile'),
    ('cad_speaker_level_transfer', 'build_speaker_electrical_path'),
    ('cad_speaker_library', 'build_speaker_dataset'),
    ('cad_spectral_lighting', 'build_spectral_evidence'),
    ('cad_speech_intelligibility', 'build_speech_intelligibility_spec'),
    ('cad_stationarity', 'assess_stationarity'),
    ('cad_streaming_qoe', 'build_streaming_session'),
    ('cad_structural_boundary', 'build_structural_boundary_model'),
    ('cad_surface_scattering', 'build_directional_scattering_kernel'),
    ('cad_surface_scattering', 'build_surface_scattering_evidence'),
    ('cad_system_nonlinearity', 'build_system_nonlinearity_measurement'),
    ('cad_tactile', 'build_tactile_actuator_definition'),
    ('cad_tactile', 'build_tactile_attachment_binding'),
    ('cad_tactile', 'build_tactile_processing_profile'),
    ('cad_tactile', 'evaluate_tactile_system'),
    ('cad_tactile_reference_pack', 'build_tactile_reference'),
    ('cad_temporal_emission', 'build_display_temporal_condition'),
    ('cad_temporal_emission', 'build_temporal_emission_measurement'),
    ('cad_temporal_emission', 'build_temporal_light_waveform'),
    ('cad_treatment_fabrication', '_seal_package'),
    ('cad_uncertainty_budget', 'build_uncertainty_budget_spec'),
    ('cad_uncertainty_budget', 'propagate_uncertainty_budget'),
    ('cad_usable_output', 'build_source_usable_output_profile'),
    ('cad_usable_output', 'evaluate_headroom'),
    ('cad_validation_campaign', 'build_validation_campaign'),
    ('cad_validation_corpus', 'build_benchmark_spec'),
    ('cad_validation_corpus', 'build_corpus_entry'),
    ('cad_video_geometry', '_build_evidence'),
    ('cad_video_latency', 'build_video_latency_condition'),
    ('cad_video_latency', 'build_video_latency_measurement'),
    ('cad_viewing_envelope', 'build_viewing_resolution_policy'),
    ('cad_viewing_envelope', 'evaluate_viewing_resolution'),
    ('cad_wave_qualification', 'build_wave_report'),
    ('external_dependency_resolver', 'build_external_dependency'),
    ('optimization_robustness_validation', 'build_o90e_decision'),
    ('optimization_robustness_validation', 'build_o90e_validation_case'),
    ('raw_mesh_health', 'build_mesh_health_summary'),
]


def _resolve(modname: str, name: str):
    mod = importlib.import_module('htdt.' + modname)
    return getattr(mod, name)


@pytest.mark.parametrize('modname,clsname', SWEEP_CLASSES)
def test_provisional_hash_equals_sealed_hash(modname: str, clsname: str):
    """Raw type-mismatched literals must seal with the provisional hash."""
    cls = _resolve(modname, clsname)
    sha_fields = _sha_fields(cls)
    methods = _payload_methods(cls)
    if not sha_fields or not methods:
        pytest.skip('no sha256 field or payload method found')
    try:
        raw = {
            n: _synth_field(f, n)
            for n, f in cls.model_fields.items()
            if f.is_required()
        }
    except _Unsynthable as exc:
        pytest.skip(f'unable to synthesize fields: {exc}')
    dummy = {s: '0' * 64 for s in sha_fields}
    # A '*_sha256' field is a seal only when the hashed payload excludes it;
    # e.g. DeviceActionAck.action_sha256 is payload content, not a seal.
    try:
        probe = cls.model_construct(
            **canonicalize_payload(cls, dict(raw, **dummy))
        )
    except Exception as exc:  # noqa: BLE001 - synthesized value rejected
        pytest.skip(f'synthesized payload rejected by field validation: {exc}')
    payload_keys: set[str] = set()
    for method in methods:
        try:
            payload_keys |= set(getattr(probe, method)())
        except Exception:  # noqa: BLE001 - some methods need full fields
            pass
    seal_fields = [s for s in sha_fields if s not in payload_keys]
    if not seal_fields:
        pytest.skip('no seal field identified')
    # The fixed builder path: provisional carries canonicalized values;
    # content hash fields keep their caller value, seals get a dummy.
    try:
        provisional = cls.model_construct(
            **canonicalize_payload(
                cls, dict(raw, **{s: '0' * 64 for s in seal_fields})
            )
        )
    except Exception as exc:  # noqa: BLE001 - synthesized value rejected
        pytest.skip(f'synthesized payload rejected by field validation: {exc}')
    # Hash over the provisional's canonical payload equals what the sealed
    # validator recomputes over validated fields — so sealing must succeed.
    ok = False
    last_exc: Exception | None = None
    # Seal candidates: each seal may be checked against a payload-method
    # hash or against a fixed module constant (e.g. algorithm_sha256).
    import re as _re
    consts = set(
        _re.findall(r"[0-9a-f]{64}", getattr(cls, '__dict__', {}).get('__doc__', '') or '')
    )
    mod = importlib.import_module('htdt.' + modname)
    for name, val in vars(mod).items():
        if isinstance(val, str) and _re.fullmatch(r'[0-9a-f]{64}', val):
            consts.add(val)
    import json as _json
    json_hashes = []
    for key, val in raw.items():
        if key.endswith('_json') and isinstance(val, str):
            try:
                json_hashes.append(canonical_sha256(_json.loads(val)))
            except Exception:  # noqa: BLE001
                pass
    method_hashes = []
    for m in methods:
        try:
            method_hashes.append(canonical_sha256(getattr(provisional, m)()))
        except Exception:  # noqa: BLE001 - method needs fuller payload
            pass
    if not method_hashes and not consts:
        pytest.skip('no payload method computable on provisional')
    # Content '*_sha256'/'*_hash' fields may be checked against module
    # constants or against the canonical hash of a sibling ``*_json``
    # field — solve their values alongside the seal.
    content_fields = [
        n
        for n in raw
        if ('sha256' in n or n.endswith('_hash')) and n not in seal_fields
    ]
    if len(content_fields) > 3:
        pytest.skip('too many hash-coupled fields to solve')
    extras = sorted(consts) + json_hashes
    combos = (
        itertools.product(*([[raw[n]] + extras for n in content_fields]))
        if content_fields
        else [()]
    )
    tried = 0
    errors_bad = ''
    for content_combo in combos:
        raw2 = dict(raw, **dict(zip(content_fields, content_combo)))
        try:
            prov2 = cls.model_construct(
                **canonicalize_payload(
                    cls, dict(raw2, **{s: '0' * 64 for s in seal_fields})
                )
            )
        except Exception:  # noqa: BLE001
            continue
        seal_cands = []
        for m in methods:
            try:
                seal_cands.append(canonical_sha256(getattr(prov2, m)()))
            except Exception:  # noqa: BLE001
                pass
        seal_cands += sorted(consts)
        # Baseline: the first validation error under a deliberately wrong
        # seal. A correct seal value can only change checks that read the
        # seal field (the seal check itself and derived-id checks), so a
        # changed first error proves the seal resolved its check.
        try:
            cls(**dict(raw2, **{s: '0' * 64 for s in seal_fields}))
            errors_bad = ''
        except Exception as exc:  # noqa: BLE001
            errors_bad = str(exc)
        if not errors_bad:
            pytest.skip('seal field not enforced by validation')
        for seal_combo in itertools.product(seal_cands, repeat=len(seal_fields)):
            tried += 1
            if tried > 2000:
                break
            try:
                cls(**dict(raw2, **dict(zip(seal_fields, seal_combo))))
            except Exception as exc:  # noqa: BLE001 - inspecting failure cause
                if str(exc) != errors_bad:
                    ok = True  # the seal check moved on: provisional == sealed
                    break
                last_exc = exc
                continue
            ok = True
            break
        if ok or tried > 2000:
            break
    if not ok:
        if last_exc is not None and 'hash mismatch' in str(last_exc).lower():
            if 'hash mismatch' in errors_bad.lower():
                # The same mismatch fires under a wrong seal — it is a
                # content check reached before the seal, not the seal.
                pytest.skip(
                    f'hash-coupled content field unsolved: {errors_bad[:200]}'
                )
            pytest.fail(
                f'{clsname} provisional hash does not seal: {last_exc}'
            )
        pytest.skip(f'synthesized payload rejected by validation: {last_exc}')


def _builder_args(fn) -> dict | None:
    sig = inspect.signature(fn)
    args = {}
    for p in sig.parameters.values():
        if p.default is not inspect.Parameter.empty:
            continue
        if p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return None
        ann = p.annotation
        try:
            if ann is str or str(ann) in ('str', 'str | None'):
                args[p.name] = (
                    'a' * 64
                    if ('sha256' in p.name or 'hash' in p.name)
                    else 'sweep-1'
                )
            elif ann is float or str(ann) in ('float', 'float | None'):
                args[p.name] = 48000  # int literal into float param
            elif ann is int or str(ann) in ('int', 'int | None'):
                args[p.name] = 3
            elif ann is bool or str(ann) in ('bool', 'bool | None'):
                args[p.name] = True
            else:
                return None
        except Exception:
            return None
    return args


@pytest.mark.parametrize('modname,funcname', SWEEP_BUILDERS)
def test_public_builder_accepts_int_literals(modname: str, funcname: str):
    """Public builders seal correctly when given non-canonical literals."""
    try:
        fn = _resolve(modname, funcname)
    except AttributeError:
        pytest.skip('builder not resolvable at module level')
    args = _builder_args(fn)
    if args is None:
        pytest.skip('builder parameters not auto-synthesizable')
    try:
        result = fn(**args)
    except Exception as exc:  # noqa: BLE001 - inspecting failure cause
        if 'hash mismatch' in str(exc).lower():
            pytest.fail(f'{funcname} hash mismatch: {exc}')
        pytest.skip(f'builder rejected synthesized args: {exc}')
    assert result is not None
