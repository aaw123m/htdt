"""REV55-REGCAL service: derive, persist and serve registrations (#564).

The service assembles a ``PredictionMeasurementRegistration`` from the
persisted authorities that already own each fact — the measurement record
(declared position, revision binding, routing scope), its verified datasets
(frequency domain, level reference, phase validity), the persisted quality
report (acquisition context -> timing reference + microphone calibration),
the IR dataset (t0 semantics, windowing), and the prediction artifact
(provider response or imported predicted-evidence dataset). Nothing is
re-derived silently: anything the persisted authorities do not supply stays
an explicit ``unknown`` on the registration.

Residual computation resolves the same authorities at call time and refuses
outright when the registration's sealed verdict is not comparable.
"""

from __future__ import annotations

import json
from typing import Any

from .cad_equipment import FrequencyDomain
from .cad_prediction_measurement_registration import (
    ComparabilityState,
    EnvironmentRegistration,
    LevelRegistration,
    LocalFrameBinding,
    ManualPositionCorrection,
    MeasurementAuthorityBinding,
    PredictionAuthorityBinding,
    PredictionMeasurementRegistration,
    PredictionMeasurementResidualReport,
    ProcessingOperation,
    RegistrationEndpoint,
    RegistrationPartition,
    SpatialRegistration,
    SpatialRegistrationMethod,
    TimingRegistration,
    TimeReferenceMethod,
    assert_partition_disjoint,
    build_prediction_measurement_registration,
    compute_residual_report,
    evaluate_registration_freshness,
)
from .cad_prediction_measurement_registration_repository import (
    CadPredictionMeasurementRegistrationRepository,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_prediction_repository import CadPredictionRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import acoustic_reference_position
from .clock import utc_now_iso as _utc_now
from .comparison import FrequencyResponse


class PredictionMeasurementError(ValueError):
    """Registration/resolution failures surfaced to callers."""


_TIMING_METHOD_BY_SEMANTIC: dict[str, TimeReferenceMethod] = {
    'acoustic_reference': 'acoustic_timing_reference',
    'loopback': 'exact_reference',
    'shared_clock': 'exact_reference',
    'external_sync': 'exact_reference',
    # imported/manual declarations exist but are unverified — they map to
    # 'unknown' so the gate keeps the pair limited instead of pretending an
    # unverifiable declaration is a verified reference.
    'imported': 'unknown',
    'manual': 'unknown',
    'unknown': 'unknown',
}

_LEVEL_STATE_BY_KIND: dict[str, str] = {
    'absolute_spl': 'absolute_calibrated',
    'spl_uncalibrated': 'absolute_uncalibrated',
    'dbfs': 'relative_only',
    'pa': 'relative_only',
    'relative': 'relative_only',
    'unknown': 'unknown',
}


class PredictionMeasurementService:
    """Derives registrations from persisted authorities for one document."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        measurement_repository: CadMeasurementRepository | None = None,
        quality_repository: CadMeasurementQualityRepository | None = None,
        prediction_repository: CadPredictionRepository | None = None,
        registration_repository: (
            CadPredictionMeasurementRegistrationRepository | None
        ) = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.measurement_repository = (
            measurement_repository
            if measurement_repository is not None
            else CadMeasurementRepository(scene_repository)
        )
        self.quality_repository = quality_repository
        self.prediction_repository = (
            prediction_repository
            if prediction_repository is not None
            else CadPredictionRepository(scene_repository)
        )
        self.registration_repository = (
            registration_repository
            if registration_repository is not None
            else CadPredictionMeasurementRegistrationRepository(scene_repository)
        )

    # -- derivation -------------------------------------------------------

    def build_registration(
        self,
        measurement_id: str,
        *,
        prediction_id: str | None = None,
        prediction_dataset_id: str | None = None,
        dataset_id: str | None = None,
        spatial_method: SpatialRegistrationMethod = 'exact_scene_xyz',
        spatial_provenance: str = 'manual',
        position_tolerance_m: float | None = None,
        position_uncertainty_m: float | None = None,
        local_frame: LocalFrameBinding | None = None,
        manual_correction: ManualPositionCorrection | None = None,
        named_position_entity_id: str | None = None,
        measured_source_position: tuple[float, float, float] | None = None,
        measured_receiver_position: tuple[float, float, float] | None = None,
        measured_frame: str = 'scene',
        timing_method: TimeReferenceMethod | None = None,
        applied_offset_s: float | None = None,
        timing_uncertainty_s: float | None = None,
        reference_channel: str | None = None,
        reference_event: str | None = None,
        propagation_delay_s: float | None = None,
        hardware_latency_s: float | None = None,
        ir_time_zero_offset_s: float | None = None,
        measured_sound_speed_m_s: float | None = None,
        measured_temperature_c: float | None = None,
        measured_humidity_percent: float | None = None,
        partition: RegistrationPartition = 'unassigned',
        campaign_id: str | None = None,
        registration_method: str = 'manual_pair_registration',
        confidence_label: str | None = None,
        provenance_json: str = '{}',
        created_at_utc: str | None = None,
    ) -> PredictionMeasurementRegistration:
        """Assemble a sealed registration from persisted authorities.

        ``prediction_id`` binds a persisted ``CadPredictionResult``;
        ``prediction_dataset_id`` binds an imported dataset carried by a
        ``CadMeasurementRecord`` with ``evidence_type='predicted'``. Exactly
        one must be supplied.
        """

        if (prediction_id is None) == (prediction_dataset_id is None):
            raise PredictionMeasurementError(
                'exactly one prediction artifact must be bound'
            )
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None or record.document_id != self.document_id:
            raise PredictionMeasurementError('測定レコードが見つかりません')
        revision = self.scene_repository.get(record.scene_revision_id)
        if revision is None:
            raise PredictionMeasurementError('測定が参照するシーンリビジョンがありません')

        dataset = self._resolve_dataset(measurement_id, dataset_id)
        ir_dataset = self._resolve_ir_dataset(measurement_id)
        report = (
            self.quality_repository.latest_report(measurement_id)
            if self.quality_repository is not None
            else None
        )
        acquisition_context = self._resolve_acquisition_context(report)
        timing_reference = self._resolve_timing_reference(acquisition_context)
        level_reference = (
            self.quality_repository.get_dataset_level_reference(dataset.dataset_id)
            if self.quality_repository is not None and dataset is not None
            else None
        )

        prediction_binding = self._resolve_prediction(
            prediction_id=prediction_id,
            prediction_dataset_id=prediction_dataset_id,
            measurement_record=record,
        )
        entities = {e.entity_id: e for e in revision.document.entities}

        measurement_binding = MeasurementAuthorityBinding(
            measurement_id=record.measurement_id,
            scene_revision_id=record.scene_revision_id,
            scene_content_hash=record.scene_content_hash,
            dataset_id=dataset.dataset_id if dataset is not None else None,
            dataset_sha256=dataset.dataset_sha256 if dataset is not None else None,
            ir_dataset_id=ir_dataset.dataset_id if ir_dataset is not None else None,
            ir_dataset_sha256=(
                ir_dataset.dataset_sha256 if ir_dataset is not None else None
            ),
            acquisition_context_id=(
                report.acquisition_context.acquisition_context_id
                if report is not None and report.acquisition_context is not None
                else None
            ),
            acquisition_context_sha256=(
                report.acquisition_context.acquisition_context_sha256
                if report is not None and report.acquisition_context is not None
                else None
            ),
            timing_reference_id=(
                acquisition_context.timing_reference_id
                if acquisition_context is not None
                else None
            ),
            timing_reference_sha256=(
                acquisition_context.timing_reference_sha256
                if acquisition_context is not None
                else None
            ),
            level_reference_id=(
                level_reference.level_reference_id if level_reference else None
            ),
            level_reference_sha256=(
                level_reference.level_reference_sha256 if level_reference else None
            ),
            measured_frequency_domain=(
                FrequencyDomain(
                    minimum_hz=min(dataset.frequency_hz),
                    maximum_hz=max(dataset.frequency_hz),
                )
                if dataset is not None
                else None
            ),
            radiation_scope=record.radiation_scope,
            routing_evidence=record.routing_evidence,
            evidence_type=record.evidence_type,
        )

        predicted_receiver = self._entity_position(
            entities, prediction_binding.receiver_entity_id
        )
        predicted_source_ids = prediction_binding.source_entity_ids
        predicted_source = (
            self._entity_position(entities, predicted_source_ids[0])
            if predicted_source_ids
            else None
        )
        source_entity = entities.get(predicted_source_ids[0]) if predicted_source_ids else None
        receiver_entity = (
            entities.get(prediction_binding.receiver_entity_id)
            if prediction_binding.receiver_entity_id is not None
            else None
        )

        measured_receiver = (
            _as_position(measured_receiver_position)
            if measured_receiver_position is not None
            else (
                entities[named_position_entity_id].position
                if named_position_entity_id
                and named_position_entity_id in entities
                else record.measurement_position
            )
        )
        measured_source = (
            _as_position(measured_source_position)
            if measured_source_position is not None
            else None
        )
        measured_receiver_declared = measured_receiver
        measured_source_declared = measured_source

        # Transform local-frame measured positions into the scene frame
        # first — manual corrections are declared in scene coordinates.
        if measured_frame == 'local_frame' and local_frame is not None:
            if measured_receiver is not None:
                measured_receiver = local_frame.to_scene(measured_receiver)
            if measured_source is not None:
                measured_source = local_frame.to_scene(measured_source)

        # Then apply a declared manual correction (the correction's
        # provenance and bound live on the record — never hidden inside
        # solver parameters).
        if manual_correction is not None and measured_receiver is not None:
            from .cad_scene import Position3

            dx, dy, dz = manual_correction.applied_offset_m
            measured_receiver = Position3(
                x_m=measured_receiver.x_m + dx,
                y_m=measured_receiver.y_m + dy,
                z_m=measured_receiver.z_m + dz,
            )

        source = RegistrationEndpoint(
            role='source',
            entity_id=predicted_source_ids[0] if predicted_source_ids else None,
            predicted_position=predicted_source,
            predicted_orientation=(
                source_entity.orientation if source_entity is not None else None
            ),
            measured_position=measured_source,
            measured_position_declared=measured_source_declared,
            position_uncertainty_m=None,
            position_delta_m=_delta_or_none(measured_source, predicted_source),
        )
        receiver = RegistrationEndpoint(
            role='receiver',
            entity_id=(
                prediction_binding.receiver_entity_id or record.measurement_entity_id
            ),
            predicted_position=predicted_receiver,
            predicted_orientation=(
                receiver_entity.orientation if receiver_entity is not None else None
            ),
            measured_position=measured_receiver,
            measured_position_declared=measured_receiver_declared,
            position_uncertainty_m=position_uncertainty_m,
            position_delta_m=_delta_or_none(measured_receiver, predicted_receiver),
        )

        spatial = SpatialRegistration(
            method=spatial_method,
            measurement_frame=measured_frame,
            local_frame=local_frame,
            manual_correction=manual_correction,
            named_position_entity_id=named_position_entity_id,
            provenance=spatial_provenance,
            position_tolerance_m=position_tolerance_m,
        )

        timing = self._derive_timing(
            timing_reference=timing_reference,
            ir_dataset=ir_dataset,
            method_override=timing_method,
            applied_offset_s=applied_offset_s,
            timing_uncertainty_s=timing_uncertainty_s,
            reference_channel=reference_channel,
            reference_event=reference_event,
            propagation_delay_s=propagation_delay_s,
            hardware_latency_s=hardware_latency_s,
            ir_time_zero_offset_s=ir_time_zero_offset_s,
            dataset=dataset,
        )

        level = self._derive_level(
            dataset=dataset,
            level_reference=level_reference,
            acquisition_context=acquisition_context,
        )

        environment = self._derive_environment(
            prediction=prediction_binding,
            measured_sound_speed_m_s=measured_sound_speed_m_s,
            measured_temperature_c=measured_temperature_c,
            measured_humidity_percent=measured_humidity_percent,
            prediction_id=prediction_id,
        )

        operations = self._derive_operations(dataset=dataset, ir_dataset=ir_dataset)

        return build_prediction_measurement_registration(
            document_id=self.document_id,
            scene_revision_id=record.scene_revision_id,
            scene_content_hash=record.scene_content_hash,
            prediction=prediction_binding,
            measurement=measurement_binding,
            source=source,
            receiver=receiver,
            spatial=spatial,
            timing=timing,
            level=level,
            environment=environment,
            processing_operations=operations,
            partition=partition,
            campaign_id=campaign_id,
            registration_method=registration_method,
            confidence_label=confidence_label,
            provenance_json=provenance_json,
            created_at_utc=created_at_utc or _utc_now(),
        )

    def register_pair(
        self, measurement_id: str, **kwargs: Any
    ) -> PredictionMeasurementRegistration:
        """Build + persist a registration for one measurement<->prediction pair."""

        registration = self.build_registration(measurement_id, **kwargs)
        self.registration_repository.save(registration)
        return registration

    # -- read / staleness -------------------------------------------------

    def list_registrations(self) -> tuple[PredictionMeasurementRegistration, ...]:
        return self.registration_repository.list_for_document(self.document_id)

    def registration_freshness(
        self, registration: PredictionMeasurementRegistration
    ) -> str:
        return self.registration_repository.evaluate_freshness(registration)

    def check_partition_disjoint(self) -> str | None:
        try:
            assert_partition_disjoint(list(self.list_registrations()))
        except ValueError as exc:
            return str(exc)
        return None

    # -- residuals ---------------------------------------------------------

    def compute_and_persist_residual_report(
        self,
        registration_id: str,
        *,
        created_at_utc: str | None = None,
        spec_overrides: dict[str, Any] | None = None,
    ) -> PredictionMeasurementResidualReport:
        registration = self.registration_repository.get(registration_id)
        if registration is None:
            raise PredictionMeasurementError('登録レコードが見つかりません')
        freshness = self.registration_repository.evaluate_freshness(registration)
        if freshness != 'current':
            raise PredictionMeasurementError(
                '登録は古いシーンリビジョンに紐づいています（stale）'
            )

        measured_response, measured_phase = self._measured_response(registration)
        predicted_response, predicted_phase = self._predicted_response(registration)
        measured_ir = self._measured_ir(registration)
        predicted_direct_delay = self._predicted_direct_delay(registration)
        predicted_reflections = self._predicted_reflection_delays(registration)
        predicted_modes = self._predicted_mode_frequencies(registration)

        report = compute_residual_report(
            registration,
            predicted_response=predicted_response,
            measured_response=measured_response,
            measured_phase_deg=measured_phase,
            predicted_phase_deg=predicted_phase,
            measured_ir=measured_ir,
            predicted_direct_delay_s=predicted_direct_delay,
            predicted_reflection_delays_s=predicted_reflections,
            predicted_mode_frequencies_hz=predicted_modes,
            spec_overrides=spec_overrides,
            created_at_utc=created_at_utc or _utc_now(),
        )
        self.registration_repository.save_report(report)
        return report

    def residual_reports(
        self, registration_id: str
    ) -> tuple[PredictionMeasurementResidualReport, ...]:
        return self.registration_repository.list_reports(registration_id)

    # -- derivation internals -------------------------------------------

    def _resolve_dataset(self, measurement_id: str, dataset_id: str | None):
        if dataset_id is not None:
            dataset = self.measurement_repository.get_dataset(dataset_id)
            if dataset is None or dataset.measurement_id != measurement_id:
                raise PredictionMeasurementError(
                    '指定されたデータセットはこの測定に属しません'
                )
            return dataset
        return self.measurement_repository.dataset_for_measurement(measurement_id)

    def _resolve_ir_dataset(self, measurement_id: str):
        datasets = self.measurement_repository.ir_datasets_for_measurement(
            measurement_id
        )
        return datasets[0] if datasets else None

    def _resolve_acquisition_context(self, report):
        if (
            self.quality_repository is None
            or report is None
            or report.acquisition_context is None
        ):
            return None
        return self.quality_repository.get_acquisition_context(
            report.acquisition_context.acquisition_context_id
        )

    def _resolve_timing_reference(self, context):
        if (
            self.quality_repository is None
            or context is None
            or context.timing_reference_id is None
        ):
            return None
        return self.quality_repository.get_timing_reference(
            context.timing_reference_id
        )

    def _resolve_prediction(
        self,
        *,
        prediction_id: str | None,
        prediction_dataset_id: str | None,
        measurement_record,
    ) -> PredictionAuthorityBinding:
        if prediction_id is not None:
            result = self.prediction_repository.get(prediction_id)
            if result is None or result.document_id != self.document_id:
                raise PredictionMeasurementError('予測結果が見つかりません')
            provider = result.provider_response
            return PredictionAuthorityBinding(
                kind='prediction_result',
                prediction_id=result.prediction_id,
                prediction_sha256=result.result_sha256,
                scene_revision_id=result.scene_revision_id,
                scene_content_hash=result.scene_content_hash,
                model_id=result.model_id,
                model_version=result.model_version,
                provider_id=provider.provider_id if provider else None,
                provider_evidence_state=(
                    provider.provider_evidence_state if provider else None
                ),
                source_entity_ids=tuple(measurement_record.source_speaker_ids),
                receiver_entity_id=(
                    provider.receiver_entity_id if provider else None
                ),
                predicted_frequency_domain=(
                    FrequencyDomain(
                        minimum_hz=min(provider.frequency_hz),
                        maximum_hz=max(provider.frequency_hz),
                    )
                    if provider is not None
                    else None
                ),
                fidelity_state=result.geometry_compatibility,
            )

        predicted_record = self._predicted_record(prediction_dataset_id)
        dataset = self.measurement_repository.dataset_for_measurement(
            predicted_record.measurement_id
        )
        if dataset is None or dataset.dataset_id != prediction_dataset_id:
            raise PredictionMeasurementError(
                '予測データセットが見つかりません'
            )
        return PredictionAuthorityBinding(
            kind='imported_prediction_dataset',
            prediction_id=dataset.dataset_id,
            prediction_sha256=dataset.dataset_sha256,
            scene_revision_id=predicted_record.scene_revision_id,
            scene_content_hash=predicted_record.scene_content_hash,
            model_id=None,
            model_version=None,
            provider_id=None,
            provider_evidence_state=None,
            source_entity_ids=tuple(predicted_record.source_speaker_ids),
            receiver_entity_id=predicted_record.measurement_entity_id,
            predicted_frequency_domain=FrequencyDomain(
                minimum_hz=min(dataset.frequency_hz),
                maximum_hz=max(dataset.frequency_hz),
            ),
            fidelity_state='imported_prediction_dataset',
        )

    def _predicted_record(self, prediction_dataset_id: str):
        dataset = self.measurement_repository.get_dataset(prediction_dataset_id)
        if dataset is None:
            raise PredictionMeasurementError('予測データセットが見つかりません')
        record = self.measurement_repository.get_measurement(dataset.measurement_id)
        if (
            record is None
            or record.document_id != self.document_id
            or record.evidence_type != 'predicted'
        ):
            raise PredictionMeasurementError(
                '予測データセットの evidence_type が predicted ではありません'
            )
        return record

    def _entity_position(self, entities, entity_id: str | None):
        if entity_id is None or entity_id not in entities:
            return None
        return acoustic_reference_position(entities[entity_id])

    def _derive_timing(
        self,
        *,
        timing_reference,
        ir_dataset,
        method_override,
        applied_offset_s,
        timing_uncertainty_s,
        reference_channel,
        reference_event,
        propagation_delay_s,
        hardware_latency_s,
        ir_time_zero_offset_s,
        dataset,
    ) -> TimingRegistration:
        declared_semantics: str | None = None
        method: TimeReferenceMethod = 'unknown'
        if timing_reference is not None:
            declared_semantics = timing_reference.method
            method = _TIMING_METHOD_BY_SEMANTIC.get(
                timing_reference.method, 'unknown'
            )
        if method_override is not None:
            method = method_override

        phase_validity: str
        if dataset is None or dataset.phase_status in ('absent', None):
            phase_validity = 'absent'
        elif dataset.phase_status == 'valid':
            phase_validity = 'valid'
        else:
            phase_validity = 'unknown'

        t0_semantics = ir_dataset.t0_semantics if ir_dataset is not None else 'unknown'

        return TimingRegistration(
            method=method,
            applied_offset_s=applied_offset_s,
            reference_channel=reference_channel,
            reference_event=reference_event,
            propagation_delay_s=propagation_delay_s,
            hardware_latency_s=hardware_latency_s,
            ir_time_zero_offset_s=ir_time_zero_offset_s,
            uncertainty_s=timing_uncertainty_s,
            reference_authority_id=(
                timing_reference.timing_reference_id if timing_reference else None
            ),
            reference_authority_sha256=(
                timing_reference.timing_reference_sha256 if timing_reference else None
            ),
            declared_reference_semantics=declared_semantics,
            t0_semantics=t0_semantics,
            absolute_phase_valid=phase_validity,
        )

    def _derive_level(
        self,
        *,
        dataset,
        level_reference,
        acquisition_context,
    ) -> LevelRegistration:
        level_state = 'unknown'
        declared_kind: str | None = None
        if level_reference is not None:
            declared_kind = level_reference.level_reference_kind
            level_state = _LEVEL_STATE_BY_KIND.get(
                level_reference.level_reference_kind, 'unknown'
            )
        elif dataset is not None and dataset.level_reference not in (None, 'unknown'):
            declared_kind = dataset.level_reference
            level_state = _LEVEL_STATE_BY_KIND.get(
                dataset.level_reference, 'unknown'
            )

        normalization = 'unknown'
        if dataset is not None:
            normalization = 'none'

        microphone = (
            acquisition_context.microphone if acquisition_context is not None else None
        )
        return LevelRegistration(
            level_state=level_state,
            level_offset_db=None,
            normalization_state=normalization,
            mic_calibration_profile=(
                microphone.calibration_profile if microphone else None
            ),
            mic_calibration_sha256=(
                microphone.calibration_sha256 if microphone else None
            ),
            soundcard_calibration_state='unknown',
            gain_routing_state=None,
            level_reference_authority_id=(
                level_reference.level_reference_id if level_reference else None
            ),
            level_reference_authority_sha256=(
                level_reference.level_reference_sha256 if level_reference else None
            ),
            declared_level_reference_kind=declared_kind,
        )

    def _derive_environment(
        self,
        *,
        prediction: PredictionAuthorityBinding,
        measured_sound_speed_m_s: float | None,
        measured_temperature_c: float | None,
        measured_humidity_percent: float | None,
        prediction_id: str | None,
    ) -> EnvironmentRegistration:
        prediction_speed: float | None = None
        prediction_temperature: float | None = None
        if prediction_id is not None:
            result = self.prediction_repository.get(prediction_id)
            if result is not None:
                prediction_speed, prediction_temperature = (
                    self._snapshot_environment(result)
                )
        return EnvironmentRegistration(
            sound_speed_m_s=measured_sound_speed_m_s,
            temperature_c=measured_temperature_c,
            relative_humidity_percent=measured_humidity_percent,
            prediction_sound_speed_m_s=prediction_speed,
            prediction_temperature_c=prediction_temperature,
            sound_speed_relative_difference=(
                abs(measured_sound_speed_m_s - prediction_speed) / prediction_speed
                if measured_sound_speed_m_s is not None
                and prediction_speed is not None
                else None
            ),
        )

    def _snapshot_environment(self, result) -> tuple[float | None, float | None]:
        try:
            snapshot = json.loads(result.input_snapshot_json)
        except (TypeError, ValueError):
            return None, None
        environment = snapshot.get('environment') if isinstance(snapshot, dict) else None
        if not isinstance(environment, dict):
            return None, None
        speed = environment.get('sound_speed_m_s')
        temperature = environment.get('temperature_c')
        return (
            float(speed) if isinstance(speed, (int, float)) else None,
            float(temperature) if isinstance(temperature, (int, float)) else None,
        )

    def _derive_operations(self, *, dataset, ir_dataset) -> tuple[ProcessingOperation, ...]:
        operations: list[ProcessingOperation] = []
        if dataset is not None:
            if dataset.smoothing not in (None, 'none', 'unknown'):
                operations.append(
                    ProcessingOperation(
                        kind='smoothing',
                        label='frequency-response smoothing',
                        parameters_json=json.dumps({'smoothing': dataset.smoothing}),
                        provenance='imported',
                    )
                )
            processing = _parse_json_dict(dataset.processing_json)
            for key in ('windowing', 'gating', 'resample', 'resampling'):
                if key in processing:
                    operations.append(
                        ProcessingOperation(
                            kind=(
                                'window'
                                if key == 'windowing'
                                else 'gate' if key == 'gating' else 'resample'
                            ),
                            label=key,
                            parameters_json=json.dumps(processing[key]),
                            provenance='imported',
                        )
                    )
        if ir_dataset is not None:
            if ir_dataset.window_kind not in (None, 'unknown'):
                operations.append(
                    ProcessingOperation(
                        kind='window',
                        label='ir window',
                        parameters_json=json.dumps({'window_kind': ir_dataset.window_kind}),
                        provenance='imported',
                    )
                )
            if ir_dataset.normalized:
                operations.append(
                    ProcessingOperation(
                        kind='normalization',
                        label='ir normalization',
                        parameters_json='{}',
                        provenance='imported',
                    )
                )
        return tuple(operations)

    # -- residual resolution -------------------------------------------

    def _measured_response(
        self, registration: PredictionMeasurementRegistration
    ) -> tuple[FrequencyResponse | None, tuple[float, ...] | None]:
        dataset_id = registration.measurement.dataset_id
        if dataset_id is None:
            return None, None
        dataset = self.measurement_repository.get_dataset(dataset_id)
        if dataset is None:
            return None, None
        return (
            FrequencyResponse(
                frequency_hz=dataset.frequency_hz,
                level_db=dataset.level_db,
            ),
            dataset.phase_deg,
        )

    def _predicted_response(
        self, registration: PredictionMeasurementRegistration
    ) -> tuple[FrequencyResponse | None, tuple[float, ...] | None]:
        binding = registration.prediction
        if binding.kind == 'prediction_result':
            result = self.prediction_repository.get(binding.prediction_id)
            provider = result.provider_response if result is not None else None
            if provider is None:
                return None, None
            return (
                FrequencyResponse(
                    frequency_hz=provider.frequency_hz,
                    level_db=provider.level_db,
                ),
                None,
            )
        if binding.kind == 'imported_prediction_dataset':
            dataset = self.measurement_repository.get_dataset(binding.prediction_id)
            if dataset is None:
                return None, None
            return (
                FrequencyResponse(
                    frequency_hz=dataset.frequency_hz,
                    level_db=dataset.level_db,
                ),
                dataset.phase_deg,
            )
        return None, None

    def _measured_ir(
        self, registration: PredictionMeasurementRegistration
    ) -> tuple[tuple[float, ...], float, float] | None:
        ir_dataset_id = registration.measurement.ir_dataset_id
        if ir_dataset_id is None:
            return None
        dataset = self.measurement_repository.get_ir_dataset(ir_dataset_id)
        if dataset is None:
            return None
        return (dataset.amplitudes, float(dataset.sample_rate_hz), float(dataset.start_time_s))

    def _predicted_direct_delay(
        self, registration: PredictionMeasurementRegistration
    ) -> float | None:
        if registration.prediction.kind != 'prediction_result':
            # Imported predicted datasets carry no geometry-derived delay:
            # the model-implied direct delay is the straight-line distance
            # under the declared sound speed.
            source = registration.source.predicted_position
            receiver = registration.receiver.predicted_position
            sound_speed = (
                registration.environment.prediction_sound_speed_m_s
                or registration.environment.sound_speed_m_s
            )
            if source is None or receiver is None or sound_speed is None:
                return None
            import math

            distance = math.sqrt(
                (source.x_m - receiver.x_m) ** 2
                + (source.y_m - receiver.y_m) ** 2
                + (source.z_m - receiver.z_m) ** 2
            )
            return distance / sound_speed
        result = self.prediction_repository.get(registration.prediction.prediction_id)
        if result is None or not result.reflections:
            return None
        sound_speed = (
            registration.environment.prediction_sound_speed_m_s
            or registration.environment.sound_speed_m_s
        )
        if sound_speed is None:
            return None
        first = min(result.reflections, key=lambda r: r.direct_length_m)
        return first.direct_length_m / sound_speed

    def _predicted_reflection_delays(
        self, registration: PredictionMeasurementRegistration
    ) -> tuple[tuple[float, str | None], ...]:
        if registration.prediction.kind != 'prediction_result':
            return ()
        result = self.prediction_repository.get(registration.prediction.prediction_id)
        if result is None or not result.reflections:
            return ()
        sound_speed = (
            registration.environment.prediction_sound_speed_m_s
            or registration.environment.sound_speed_m_s
        )
        if sound_speed is None:
            return ()
        delays: list[tuple[float, str | None]] = []
        for reflection in result.reflections:
            delay_s = reflection.reflected_length_m / sound_speed
            delays.append(
                (delay_s, f'{reflection.speaker_role}:{reflection.surface_key}')
            )
        return tuple(delays)

    def _predicted_mode_frequencies(
        self, registration: PredictionMeasurementRegistration
    ) -> tuple[float, ...]:
        if registration.prediction.kind != 'prediction_result':
            return ()
        result = self.prediction_repository.get(registration.prediction.prediction_id)
        if result is None or not result.modes:
            return ()
        return tuple(sorted(mode.frequency_hz for mode in result.modes))


def _as_position(value: tuple[float, float, float]):
    from .cad_scene import Position3

    return Position3(x_m=value[0], y_m=value[1], z_m=value[2])


def _delta_or_none(measured, predicted) -> float | None:
    if measured is None or predicted is None:
        return None
    import math

    return math.sqrt(
        (measured.x_m - predicted.x_m) ** 2
        + (measured.y_m - predicted.y_m) ** 2
        + (measured.z_m - predicted.z_m) ** 2
    )


def _parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
