# Round 13 — provisional-hash sweep

REV13-RECOMP (`docs/reviews/round13-recompute.md`) proved the sealing bug in
`cad_ir_analysis.build_ir_analysis_spec` and deferred the codebase-wide
sweep. This document is that sweep's record.

## Pattern

A builder computes the spec/identity hash from a provisional
`Model.model_construct(**payload)` — hashing the **raw caller payload** —
while the sealed model's `model_validator` recomputes the hash over
**validated fields**. Any input whose raw literal differs from its
canonical-JSON form (`48000` vs `48000.0`, `dict` vs nested model, `1` vs
`True`, `str` vs `date`) produces "hash mismatch" for otherwise valid
inputs — a documented builder that can never seal.

## Method

1. AST census of every `model_construct(` call in `backend/src/htdt`
   (316 sites) plus direct-payload-hash and probe/round-trip variants.
2. Per site, resolved the constructed class, located its hashed payload
   method (`identity_payload` / `semantic_payload` / `spec_payload`), and
   classified the dump mode it serializes:
   - `mode='json'` — serializer coerces stored values; residual hazards
     only for literal classes the JSON serializer passes through
     (float→int, int→bool, dict→model, str→datetime).
   - `mode='python'` — raw dump; **every** coerce-typed field is a hazard.
   - hand-built dicts — hazard iff the emitted value's field type coerces.
3. Every EXPOSED site was wrapped with `canonicalize_payload(cls, ...)`
   (new helper in `canonical_json.py`): each payload value is run through
   `TypeAdapter(field.annotation).validate_python` before the provisional
   is built, so the provisional's hashed payload equals the sealed
   validator's recomputed payload for every valid input. The sealed
   constructor still receives the original raw payload — error types and
   messages for invalid input are unchanged.
4. Two sites fixed by hand:
   - `cad_benchmark.py` `payload_for_hash` — direct dict hash (no
     `model_construct`); `sample_rate_hz`, `frequency_grid_hz`,
     `time_origin_s` normalized with `float()` / `[float(v) for v in ...]`.
   - `cad_adaptive_measurement_design._seal_proposal` — generic sealer
     parameterized by model class; wrapped with `canonicalize_payload`.
5. Regression proof: `backend/tests/test_round13_hash_sweep.py`
   - per-class (182 swept classes): synthesizes a raw payload of
     deliberately type-mismatched literals (int→float, float→int,
     int→bool, str→date, dict→model), then asserts a deliberately wrong
     seal and the provisional-computed seal produce different validation
     outcomes — i.e. the provisional hash IS the seal the validator
     accepts. Classes whose cross-field constraints cannot be solved
     generically are reported as skipped, not failed.
   - per-builder (183 public builders): called with synthesized scalar
     args incl. int literals for `float` params; any 'hash mismatch'
     error fails the test.

## Results

- Sites classified: 316 `model_construct` + direct-hash variants audited.
- EXPOSED: 184 sites across 120 modules — all fixed.
- SAFE: 131 sites (payload already canonical, or no coerce-typed
  field reachable via the hashed method).
- Generic `_seal_proposal` helper: fixed (covers all adaptive-measurement
  proposal builders).
- `cad_benchmark` direct-hash site: fixed.
- Tests: `backend/tests/test_round13_hash_sweep.py` — 365 parametrized
  cases; full `pytest backend/tests -n 4` passes.

## Site table

| Site | Class | Payload method kind | Verdict | Action |
|---|---|---|---|---|
| `cad_acoustic_construction.py:376` | `AcousticConstructionDefinition` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_acoustic_construction.py:504` | `DerivedMaterialAcousticEvidence` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_acoustic_index.py:283` | `AcousticIndexVariant` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_acoustic_index.py:306` | `AcousticIndexSnapshot` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_acoustic_portal_coupling.py:238` | `AcousticPortalCoupling` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_acoustic_source_pose.py:328` | `AcousticSourcePoseObservation` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_action_item.py:331` | `ProjectActionItem` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_active_lf_control.py:221` | `ActiveLowFrequencyControlPlan` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_adaptive_extended.py:648` | `CadAdaptiveExtendedPlan` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_adaptive_measurement_design.py:281` | `AdaptiveMeasurementDesignSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_adaptive_planner.py:490` | `CadAdaptivePlan` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ambient_noise.py:482` | `AmbientNoiseProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ambient_noise.py:510` | `AmbientNoiseCriterion` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ambient_noise.py:881` | `AmbientNoiseComparison` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_anechoic_programme_pack.py:330` | `AnechoicProgrammeAsset` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_audio_transform_lineage.py:128` | `AudioFormatTransform` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_audio_transform_lineage.py:262` | `TransformLineageEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_auralization.py:210` | `AuralizationRenderSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_av_session_reliability.py:142` | `AVPlaybackSession` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_av_sync.py:325` | `AVSyncCondition` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_av_sync.py:376` | `AVLatencyMeasurement` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_av_sync.py:431` | `AVLatencyMeasurement` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_bass_management.py:252` | `BassManagementProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_bass_management.py:511` | `BassManagementEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_bt2111_video_pack.py:286` | `PatternDescriptor` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_cable_run.py:205` | `CableRun` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_calibration.py:732` | `CadCalibrationPlan` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_calibration.py:1096` | `CadVerificationMeasurementPlan` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_calibration.py:1471` | `CadVerificationMeasurementCompletion` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_camilladsp.py:718` | `DeviceActionAck` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_cec_adapter.py:314` | `CECEventRecord` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_colorimetry.py:173` | `VideoColorTargetProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_colorimetry.py:314` | `VideoColorMeasurementSet` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_colorimetry.py:1088` | `VideoColorEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_correction_design_policy.py:257` | `CorrectionDesignPolicy` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_data_source_registry.py:449` | `DataSourceRegistryEntry` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_data_source_registry.py:482` | `RawSourceRecord` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_data_source_registry.py:519` | `ImporterDeclaration` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_design_comparison.py:339` | `DesignComparisonSet` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_device_backup.py:123` | `DeviceConfigurationBackupArtifact` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_direct_view.py:301` | `DirectViewDisplaySpecification` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_directivity_admission.py:168` | `DirectivityAdmissionRecord` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_drawing_set.py:201` | `DrawingSetSpec` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ebu_loudness_pack.py:130` | `LoudnessTestCase` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_equipment_device.py:588` | `DeviceActionAck` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_equipment_self_noise.py:211` | `EquipmentAcousticNoiseProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_evidence_reconciliation.py:673` | `ReconciliationDecision` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_external_admission.py:168` | `ExternalAssetFile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_external_admission.py:203` | `ExternalAssetAdmission` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_external_calibration.py:755` | `ImportedCalibrationArtifact` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_field_explorer.py:267` | `FieldExplorerSession` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_fir_filter.py:247` | `FIRFilterArtifact` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_fir_filter.py:366` | `FIRImportRecord` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_flair_bridge.py:184` | `FlairGeometryArtifact` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_flair_bridge.py:312` | `FlairBridgeCase` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_foam_material_batch.py:271` | `JcalParameterSet` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_hdmi_transport.py:208` | `HDMITransportCapability` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_hdmi_transport.py:221` | `HDMILatencyIndicationEvidence` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_hue_lighting.py:509` | `DeviceActionAck` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_input_chain_capability.py:228` | `MeasurementInputChainProfile` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_installed_surface.py:328` | `InstalledSurfaceAcousticMeasurement` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ir_analysis.py:630` | `IRAnalysisResult` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_ir_analysis.py:695` | `IRAnalysisSpec` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_isolation_assembly_pack.py:139` | `IsolationAssembly` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_joint_optimization.py:639` | `JointOptimizationSpec` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_laser_speckle.py:218` | `LaserProjectionSpeckleCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_laser_speckle.py:233` | `LaserProjectionSpeckleMeasurement` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_lighting.py:197` | `LightingScene` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_lighting.py:612` | `LightingSceneEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_listener_pose.py:201` | `ListenerPoseAuthority` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_listening_session.py:209` | `ListeningSessionSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_material_library.py:293` | `MaterialDefinition` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_material_library.py:344` | `MaterialAcousticEvidence` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measured_modal_analysis.py:258` | `MeasuredModalAnalysisSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measured_modal_analysis.py:283` | `MeasuredModalModel` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_authorities.py:510` | `CadMeasurementTimingReference` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_authorities.py:874` | `CadAcousticLevelCalibration` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_pose.py:491` | `PlannedObservedPoseDelta` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_quality.py:289` | `CadMeasurementQualityProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_quality.py:691` | `CadAcquisitionContext` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_stimulus.py:344` | `CadMeasurementExcitationAsset` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_stimulus.py:398` | `CadMeasurementStimulusProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_measurement_target_pattern.py:293` | `MeasurementTargetPattern` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_media_source_capability.py:151` | `MediaPlaybackSourceCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_meter_correction.py:368` | `MeterCorrectionArtifact` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_mic_response_calibration.py:203` | `MicrophoneResponseCalibrationProfile` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_model_calibration.py:234` | `AcousticModelCalibrationSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_model_calibration.py:1219` | `AcousticModelCalibrationResult` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_moving_mic_measurement.py:263` | `MovingMicrophoneMeasurementSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_moving_mic_measurement.py:271` | `SpatialAverageMeasurement` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_multi_radiator_source.py:484` | `RadiatorCoherenceEvaluation` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_multi_receiver_acquisition.py:228` | `MultiReceiverAcquisition` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_multi_receiver_acquisition.py:255` | `DerivedReceiverAverage` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_multi_seat_analysis.py:318` | `MultiSeatAnalysisResult` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_occupancy_acoustics.py:135` | `RoomOccupancyAcousticState` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_operating_preset.py:361` | `TheaterOperatingPreset` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_operating_preset.py:419` | `PresetMeasurementBinding` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_personal_listening.py:142` | `PersonalListeningRoute` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_phase_time_analysis.py:429` | `PhaseTimeAnalysisSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_phase_time_analysis.py:437` | `PhaseTimeAnalysisResult` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:238` | `ProjectorImagePerformanceProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:321` | `ScreenOpticalProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:465` | `AmbientReflectanceProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:909` | `ExpectedLuminanceEstimate` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:1044` | `ExpectedLuminanceEstimate` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_photometric.py:1371` | `PhotometricEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_pjlink.py:730` | `DeviceActionAck` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_playback_level.py:451` | `ReferencePlaybackProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_playback_level_compensation.py:207` | `PlaybackLevelCompensationProfile` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_prediction_matrix.py:173` | `PredictionMatrixSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_presentation_profile.py:197` | `PresentationModeConfirmation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_presentation_profile.py:279` | `VideoPresentationProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_presentation_profile.py:526` | `PresentationProfileEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_processing_condition.py:170` | `VideoProcessingCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_program_dynamics.py:112` | `ProgramDynamicsProcessingProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_program_dynamics.py:190` | `EffectiveDynamicsProcessingState` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_program_dynamics.py:355` | `DynamicsEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_program_stress.py:346` | `ProgramStressProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_project_activity.py:256` | `ProjectActivityEvent` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_projector_reference_pack.py:266` | `ProjectorReferencePack` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_rack_infrastructure.py:145` | `RackDefinition` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_reflection_diagnostic.py:87` | `ReflectionDiagnosticRequest` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_reflection_guidance.py:111` | `ReflectionGuidanceItem` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_renderer_topology.py:208` | `RendererOutputTopology` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_renderer_topology.py:370` | `RendererTopologyEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_room_reflection.py:276` | `RoomOpticalSurfaceProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_room_reflection.py:291` | `InSituContrastMeasurement` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_room_reflection.py:304` | `ProjectedContrastDecomposition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_screen_evidence_registry.py:240` | `ScreenEvidenceRecord` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_screen_evidence_registry.py:269` | `ScreenEvidenceRegistry` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_screen_moire.py:273` | `ScreenMicrostructureAuthority` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_screen_moire.py:286` | `MoireCompatibilityCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_screen_moire.py:301` | `MoireObservation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_search.py:152` | `CadSearchSpec` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_signal_path.py:329` | `AVSignalPath` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_signal_path.py:734` | `SignalPathEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_fidelity.py:250` | `ProjectionOpticalCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_fidelity.py:265` | `SpatialImageQualityMeasurement` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_field.py:148` | `SpatialFieldRequestSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_ir_measurement.py:264` | `SpatialMeasurementArrayProfile` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_ir_measurement.py:272` | `SpatialRoomImpulseResponseDataset` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_ir_metrics.py:300` | `SpatialIRMetricSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_reproduction.py:323` | `SpatialReproductionProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_starter_pack.py:122` | `SpatialStarterFixture` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_starter_pack.py:175` | `SpatialStarterPack` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spatial_uncertainty.py:164` | `SpatialObservationUncertainty` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_speaker_level_transfer.py:419` | `AmplifierOutputImpedanceAuthority` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_speaker_level_transfer.py:454` | `SpeakerCableElectricalProfile` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_speaker_level_transfer.py:487` | `SpeakerElectricalPath` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_speaker_library.py:328` | `SpeakerDataset` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_spectral_lighting.py:139` | `SpectralEvidence` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_speech_intelligibility.py:125` | `SpeechIntelligibilityAnalysisSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_stationarity.py:391` | `MeasurementStationarityAssessment` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_streaming_qoe.py:142` | `StreamingPlaybackSession` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_structural_boundary.py:287` | `StructuralBoundaryModel` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_surface_scattering.py:427` | `SurfaceScatteringEvidence` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_surface_scattering.py:464` | `DirectionalScatteringKernel` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_system_nonlinearity.py:375` | `SystemNonlinearityMeasurement` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_tactile.py:126` | `TactileActuatorDefinition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_tactile.py:186` | `TactileAttachmentBinding` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_tactile.py:251` | `TactileProcessingProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_tactile.py:603` | `TactileSystemEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_tactile_reference_pack.py:98` | `TactileActuatorReference` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_temporal_emission.py:342` | `DisplayTemporalCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_temporal_emission.py:357` | `TemporalLightWaveform` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_temporal_emission.py:376` | `TemporalEmissionMeasurement` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_treatment_fabrication.py:283` | `TreatmentFabricationPackage` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_uncertainty_budget.py:254` | `UncertaintyBudgetSpec` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_uncertainty_budget.py:415` | `UncertaintyBudgetResult` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_usable_output.py:274` | `SourceUsableOutputProfile` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_usable_output.py:765` | `HeadroomEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_validation_campaign.py:372` | `CadValidationCampaign` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_validation_corpus.py:546` | `ValidationCorpusEntry` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_validation_corpus.py:590` | `ValidationBenchmarkSpec` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_video_geometry.py:493` | `ProjectorSpecificationEvidence` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_video_latency.py:228` | `VideoLatencyCondition` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_video_latency.py:260` | `VideoLatencyMeasurement` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_viewing_envelope.py:398` | `ViewingResolutionEvaluation` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_viewing_envelope.py:428` | `ViewingResolutionPolicy` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `cad_wave_qualification.py:117` | `WaveSuiteReport` | `python` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `external_dependency_resolver.py:212` | `ExternalAuthorityDependency` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `optimization_robustness_validation.py:614` | `O90EValidationCase` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `optimization_robustness_validation.py:676` | `O90EValidationDecision` | `json` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `raw_mesh_health.py:320` | `MeshHealthSummary` | `handbuilt` | EXPOSED | fixed — `canonicalize_payload` wrap |
| `analysis_export.py:275` | `AnalysisExportBundle` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_aalto_srir_batch.py:442` | `BenchmarkSourceAsset` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_aalto_srir_batch.py:485` | `BenchmarkCase` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_acoustic_material.py:205` | `AcousticMaterialAuthority` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_acoustic_portal_coupling.py:424` | `PortalReadinessReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_action_item.py:263` | `ProjectActionItem` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_ambient_noise.py:428` | `AmbientOperatingCondition` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_ambient_noise.py:573` | `AmbientCriteriaEvaluation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_analysis_study.py:318` | `AnalysisStudy` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_applicability.py:175` | `CadApplicabilityAttestation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_assumption_decision.py:193` | `AssumptionDecision` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_calibration.py:889` | `CadCalibrationExportSnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_calibration.py:1593` | `CadCalibrationLifecycleEvent` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_calibration_workflow.py:283` | `CadAppliedSettingsRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_camilladsp.py:435` | `ImportedCalibrationArtifact` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_camilladsp.py:561` | `DeviceCapabilitySnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_camilladsp.py:625` | `ObservedDeviceState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_camilladsp.py:671` | `ProposedDeviceAction` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_commissioning.py:219` | `ToleranceProfile` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_commissioning.py:375` | `CommissioningPlan` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_commissioning.py:787` | `CommissioningRun` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_data_source_registry.py:546` | `SourceReviewDecision` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_data_source_registry.py:585` | `DatasetReviewRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_data_source_registry.py:618` | `UpstreamVersionCandidate` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_brief.py:346` | `ProjectDesignBrief` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_checkpoint.py:346` | `ConstraintWorkspaceSnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_checkpoint.py:394` | `ProjectDesignCheckpoint` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_checkpoint.py:658` | `CheckpointRestoreRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_comparison.py:308` | `ComparisonAlternative` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_design_decision.py:346` | `DesignDecisionRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_device_adapter.py:150` | `AdapterDeviceBinding` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_device_adapter.py:416` | `EffectiveAppliedSettingsSnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_device_adapter_file.py:129` | `MaterializedCalibrationSettings` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_direct_view.py:451` | `DirectViewGeometryRequest` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_direct_view.py:756` | `DirectViewGeometryEvaluation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_directivity_admission.py:99` | `CoordinateNormalizationSpec` | `python` | SAFE | verified — no fix needed |
| `cad_directivity_inspection.py:582` | `DirectivityInspectionConfirmation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_drawing_set.py:727` | `DrawingSheet` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_equipment_device.py:487` | `DeviceCapabilitySnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_equipment_device.py:521` | `ObservedDeviceState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_equipment_device.py:558` | `ProposedDeviceAction` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_equipment_device.py:624` | `DeviceTargetBinding` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_equipment_self_noise.py:408` | `EquipmentNoiseReadinessReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_evidence_register.py:410` | `ProjectEvidenceGap` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_external_calibration.py:970` | `ImportedVsExportComparison` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_field_evidence.py:237` | `FieldEvidenceRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_field_labels.py:444` | `LabelSheet` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_field_session.py:213` | `FieldSession` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_field_session.py:228` | `FieldSession` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_field_session.py:393` | `FieldEvidenceRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_foam_material_batch.py:446` | `BenchmarkSourceAsset` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_foam_material_batch.py:507` | `BenchmarkCase` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_hue_lighting.py:305` | `DeviceCapabilitySnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_hue_lighting.py:388` | `ObservedDeviceState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_hue_lighting.py:460` | `ProposedDeviceAction` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_hue_lighting.py:558` | `HueEventRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_idle_noise.py:216` | `PlaybackIdleNoiseMeasurement` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_idle_noise.py:428` | `IdleNoiseCommissioningReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_input_chain_capability.py:238` | `CaptureDynamicRangeObservation` | `json` | SAFE | verified — no fix needed |
| `cad_installation_datum.py:185` | `InstallationDatum` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_joint_execution.py:564` | `CadCalibrationPlan` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_authorities.py:970` | `CadDatasetLevelReference` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_authorities.py:1147` | `CadRoutingProfile` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_authorities.py:1579` | `CadWiringVerificationCheck` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_disposition.py:263` | `CadMeasurementCorrection` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_models.py:353` | `CadMeasurementComparison` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_models.py:355` | `CadMeasurementComparison` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_pose.py:331` | `MeasurementPoseObservation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_quality.py:751` | `CadMeasurementObservation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_quality.py:1873` | `CadMeasurementQualityReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_runner.py:288` | `MeasurementRunnerPlan` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_target_pattern.py:404` | `MaterializedPatternPoint` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_measurement_targets.py:155` | `CadMeasurementTargetLineage` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_meter_correction.py:528` | `CorrectionCompatibility` | `python` | SAFE | verified — no fix needed |
| `cad_model_validation.py:463` | `CadModelValidationRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_multi_radiator_source.py:289` | `MultiRadiatorSourceModel` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_multi_seat_analysis.py:230` | `MultiSeatAnalysisSet` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_nut_adapter.py:293` | `NUTDeviceRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_operating_preset.py:389` | `AppliedPresetState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_pjlink.py:417` | `DeviceCapabilitySnapshot` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_pjlink.py:610` | `ObservedDeviceState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_pjlink.py:670` | `ProposedDeviceAction` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_pjlink.py:788` | `PJLinkNotificationRecord` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_playback_level.py:498` | `PlaybackLevelCondition` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_power_thermal_telemetry.py:298` | `PowerThermalTelemetrySession` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_power_thermal_telemetry.py:449` | `TelemetryReadinessReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_predictions.py:473` | `CadPredictionResult` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_program_stress.py:560` | `ProgramStressEvaluation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_project_activity.py:215` | `ProjectActivityNote` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_project_template.py:334` | `ProjectTemplate` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_project_template.py:712` | `ProjectTemplateInstantiation` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_projector_reference_pack.py:212` | `ProjectorReferenceSource` | `python` | SAFE | verified — no fix needed |
| `cad_projector_reference_pack.py:239` | `ProjectorFieldAssertion` | `python` | SAFE | verified — no fix needed |
| `cad_provider_response.py:373` | `CadPredictionResult` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_provider_response.py:425` | `CadPredictionResult` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_raised_platform.py:252` | `RaisedPlatformAcousticAssembly` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_raised_platform.py:428` | `PlatformReadinessReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_reference_cases.py:215` | `ReferenceCaseManifest` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_reference_cases.py:232` | `CaseLibrary` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_review_note.py:143` | `ReviewNote` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_review_note.py:162` | `ReviewNote` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_room_operating_state.py:197` | `RoomOperatingState` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_screen_evidence_registry.py:209` | `ScreenEvidenceSource` | `python` | SAFE | verified — no fix needed |
| `cad_screen_transfer.py:298` | `AcousticScreenTransferAuthority` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_site.py:188` | `SiteSpace` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_site.py:212` | `SiteSpace` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_site.py:306` | `SpaceRelationship` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_snap.py:184` | `Position3` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_source_response.py:350` | `SourceFrequencyResponseAuthority` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_speaker_level_transfer.py:893` | `SpeakerLevelTransferResult` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_speaker_library.py:281` | `SpeakerDefinition` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_standards_registry.py:313` | `StandardsCoverageReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_stationarity.py:271` | `MeasurementStationarityScope` | `json` | SAFE | verified — no fix needed |
| `cad_structural_boundary.py:312` | `DerivedEffectiveImpedance` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_structural_boundary.py:506` | `BoundaryCapabilityReport` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_system_health.py:343` | `SystemHealthBaseline` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_system_health.py:375` | `HealthCheckPlan` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_system_health.py:715` | `HealthCheckRun` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_target_profile.py:181` | `CadTargetCurveProfile` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_target_profile.py:254` | `CalibrationPlanTargetBinding` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_validation_evidence.py:245` | `ValidationEvidenceProgram` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_validation_metrics.py:325` | `CadApplicabilityCheck` | `handbuilt` | SAFE | verified — no fix needed |
| `cad_yamaha_rxa4a.py:274` | `DeviceCompatibilityMatrix` | `handbuilt` | SAFE | verified — no fix needed |
| `edid_conformance.py:144` | `EdidCorpusEntry` | `handbuilt` | SAFE | verified — no fix needed |
| `external_dependency_resolver.py:302` | `DependencyResolutionEvent` | `handbuilt` | SAFE | verified — no fix needed |
| `raw_mesh_health.py:391` | `MeshComponentInventory` | `handbuilt` | SAFE | verified — no fix needed |
| `raw_mesh_repair.py:551` | `RepairedRawMesh` | `handbuilt` | SAFE | verified — no fix needed |
| `raw_mesh_repair.py:605` | `RepairedRawMeshDiagnosticResult` | `handbuilt` | SAFE | verified — no fix needed |
| `raw_mesh_repair.py:954` | `RawMeshRepairOperationResult` | `handbuilt` | SAFE | verified — no fix needed |
| `rew_source_context.py:241` | `RewMeasurementSourceContext` | `handbuilt` | SAFE | verified — no fix needed |
| `semantic_geometry.py:600` | `SemanticAcousticGeometry` | `handbuilt` | SAFE | verified — no fix needed |
