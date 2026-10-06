"""Append-only persistence for REV59-LOUDSPK authorities.

Nineteen tables in one repository — large-signal mechanics (#754),
source normalization (#734), thermal compression (#731), microphone
incidence (#732), same-channel arrays (#737) and grille transfer
(#735):

* ``cad_large_signal_models`` / ``cad_excursion_capabilities`` /
  ``cad_vent_flow_capabilities`` / ``cad_mechanical_output_limits``
* ``cad_source_normalizations`` / ``cad_reference_drive_conditions`` /
  ``cad_absolute_output_anchors``
* ``cad_sustained_output_tests`` /
  ``cad_thermal_compression_observations`` / ``cad_recovery_profiles``
* ``cad_microphone_directional_profiles`` /
  ``cad_receiver_orientation_states`` /
  ``cad_microphone_incidence_applicability``
* ``cad_same_channel_arrays`` / ``cad_array_reproduction_modes`` /
  ``cad_array_qualifications``
* ``cad_loudspeaker_front_layers`` / ``cad_grille_transfer_evidence`` /
  ``cad_front_layer_applicability``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_large_signal import (
    ExcursionCapability,
    LargeSignalTransducerModel,
    MechanicalOutputLimitAssessment,
    VentFlowCapability,
)
from .cad_source_normalization import (
    AbsoluteAcousticOutputAnchor,
    LoudspeakerSourceNormalization,
    ReferenceDriveCondition,
)
from .cad_thermal_compression import (
    RecoveryProfile,
    SustainedOutputTest,
    ThermalCompressionObservation,
)
from .cad_microphone_incidence import (
    MeasurementMicrophoneDirectionalProfile,
    MicrophoneIncidenceApplicability,
    ReceiverOrientationState,
)
from .cad_surround_array import (
    ArrayAcousticQualification,
    ArrayReproductionMode,
    SameChannelSpeakerArray,
)
from .cad_grille_transfer import (
    FrontLayerApplicability,
    GrilleTransferEvidence,
    LoudspeakerFrontLayer,
)


class LoudspeakerEvidenceConflictError(ValueError):
    """A loudspeaker-evidence save violated append-only identity rules."""


class LoudspeakerEvidenceIntegrityError(ValueError):
    """A stored loudspeaker-evidence row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(
        record.identity_payload()  # type: ignore[attr-defined]
    )
    if getattr(record, sha_field) != sha:
        raise LoudspeakerEvidenceIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise LoudspeakerEvidenceIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise LoudspeakerEvidenceConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise LoudspeakerEvidenceIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise LoudspeakerEvidenceIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise LoudspeakerEvidenceIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadLoudspeakerEvidenceRepository:
    """Native storage for the #754/#734/#731/#732/#737/#735 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_large_signal_models',
                'cad_excursion_capabilities',
                'cad_vent_flow_capabilities',
                'cad_mechanical_output_limits',
                'cad_source_normalizations',
                'cad_reference_drive_conditions',
                'cad_absolute_output_anchors',
                'cad_sustained_output_tests',
                'cad_thermal_compression_observations',
                'cad_recovery_profiles',
                'cad_microphone_directional_profiles',
                'cad_receiver_orientation_states',
                'cad_microphone_incidence_applicability',
                'cad_same_channel_arrays',
                'cad_array_reproduction_modes',
                'cad_array_qualifications',
                'cad_loudspeaker_front_layers',
                'cad_grille_transfer_evidence',
                'cad_front_layer_applicability',
            )
        self.large_signal_models = _SealedStore(
            self._connect, 'cad_large_signal_models',
            LargeSignalTransducerModel, 'model_id', 'model_sha256',
            (
                ('document_id', '__document_id__'),
                ('evidence_class', 'evidence_class'),
                ('enclosure_alignment', 'enclosure_alignment'),
                ('level_applicability', 'level_applicability'),
            ),
        )
        self.excursion_capabilities = _SealedStore(
            self._connect, 'cad_excursion_capabilities',
            ExcursionCapability, 'capability_id', 'capability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('transducer_ref_id', 'transducer_ref'),
                ('evidence_class', 'evidence_class'),
                ('definition_basis', 'definition_basis'),
            ),
        )
        self.vent_flow_capabilities = _SealedStore(
            self._connect, 'cad_vent_flow_capabilities',
            VentFlowCapability, 'capability_id', 'capability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('system_ref_id', 'system_ref'),
                ('mechanism', 'mechanism'),
            ),
        )
        self.output_limits = _SealedStore(
            self._connect, 'cad_mechanical_output_limits',
            MechanicalOutputLimitAssessment,
            'assessment_id', 'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('model_ref_id', 'model_ref'),
                ('limiting_mechanism', 'limiting_mechanism'),
                ('verdict', 'verdict'),
            ),
        )
        self.source_normalizations = _SealedStore(
            self._connect, 'cad_source_normalizations',
            LoudspeakerSourceNormalization,
            'normalization_id', 'normalization_sha256',
            (
                ('document_id', '__document_id__'),
                ('capability', 'capability'),
                ('normalization_method', 'normalization_method'),
            ),
        )
        self.drive_conditions = _SealedStore(
            self._connect, 'cad_reference_drive_conditions',
            ReferenceDriveCondition, 'condition_id', 'condition_sha256',
            (
                ('document_id', '__document_id__'),
                ('quantity_kind', 'quantity_kind'),
            ),
        )
        self.output_anchors = _SealedStore(
            self._connect, 'cad_absolute_output_anchors',
            AbsoluteAcousticOutputAnchor, 'anchor_id', 'anchor_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('normalization_ref_id', 'normalization_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.sustained_tests = _SealedStore(
            self._connect, 'cad_sustained_output_tests',
            SustainedOutputTest, 'test_id', 'test_sha256',
            (
                ('document_id', '__document_id__'),
                ('capability_class', 'capability_class'),
                ('initial_thermal_state', 'initial_thermal_state'),
            ),
        )
        self.compression_observations = _SealedStore(
            self._connect, 'cad_thermal_compression_observations',
            ThermalCompressionObservation,
            'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('test_ref_id', 'test_ref'),
                ('suspected_cause', 'suspected_cause'),
            ),
        )
        self.recovery_profiles = _SealedStore(
            self._connect, 'cad_recovery_profiles',
            RecoveryProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('test_ref_id', 'test_ref'),
                ('recovery_state', 'recovery_state'),
            ),
        )
        self.mic_profiles = _SealedStore(
            self._connect, 'cad_microphone_directional_profiles',
            MeasurementMicrophoneDirectionalProfile,
            'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('calibration_field_kind', 'calibration_field_kind'),
            ),
        )
        self.orientation_states = _SealedStore(
            self._connect, 'cad_receiver_orientation_states',
            ReceiverOrientationState, 'state_id', 'state_sha256',
            (
                ('document_id', '__document_id__'),
                ('orientation_frame', 'orientation_frame'),
            ),
        )
        self.incidence_applicability = _SealedStore(
            self._connect, 'cad_microphone_incidence_applicability',
            MicrophoneIncidenceApplicability,
            'applicability_id', 'applicability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.channel_arrays = _SealedStore(
            self._connect, 'cad_same_channel_arrays',
            SameChannelSpeakerArray, 'array_id', 'array_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('logical_channel_ref_id', 'logical_channel_ref'),
                ('topology', 'topology'),
            ),
        )
        self.reproduction_modes = _SealedStore(
            self._connect, 'cad_array_reproduction_modes',
            ArrayReproductionMode, 'mode_id', 'mode_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('array_ref_id', 'array_ref'),
                ('render_mode', 'render_mode'),
            ),
        )
        self.array_qualifications = _SealedStore(
            self._connect, 'cad_array_qualifications',
            ArrayAcousticQualification,
            'qualification_id', 'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('array_ref_id', 'array_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.front_layers = _SealedStore(
            self._connect, 'cad_loudspeaker_front_layers',
            LoudspeakerFrontLayer, 'layer_id', 'layer_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('loudspeaker_ref_id', 'loudspeaker_ref'),
                ('kind', 'kind'),
            ),
        )
        self.grille_transfers = _SealedStore(
            self._connect, 'cad_grille_transfer_evidence',
            GrilleTransferEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('layer_ref_id', 'layer_ref'),
                ('evidence_class', 'evidence_class'),
                ('transfer_kind', 'transfer_kind'),
            ),
        )
        self.front_layer_applicability = _SealedStore(
            self._connect, 'cad_front_layer_applicability',
            FrontLayerApplicability,
            'applicability_id', 'applicability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('layer_ref_id', 'layer_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_large_signal_model(
        self, record: LargeSignalTransducerModel
    ) -> None:
        self.large_signal_models.save(record)

    def get_large_signal_model(
        self, model_id: str
    ) -> LargeSignalTransducerModel | None:
        return self.large_signal_models.get(model_id)

    def save_excursion_capability(self, record: ExcursionCapability) -> None:
        self.excursion_capabilities.save(record)

    def get_excursion_capability(
        self, capability_id: str
    ) -> ExcursionCapability | None:
        return self.excursion_capabilities.get(capability_id)

    def save_vent_flow_capability(self, record: VentFlowCapability) -> None:
        self.vent_flow_capabilities.save(record)

    def get_vent_flow_capability(
        self, capability_id: str
    ) -> VentFlowCapability | None:
        return self.vent_flow_capabilities.get(capability_id)

    def save_output_limit(
        self, record: MechanicalOutputLimitAssessment
    ) -> None:
        self.output_limits.save(record)

    def get_output_limit(
        self, assessment_id: str
    ) -> MechanicalOutputLimitAssessment | None:
        return self.output_limits.get(assessment_id)

    def save_source_normalization(
        self, record: LoudspeakerSourceNormalization
    ) -> None:
        self.source_normalizations.save(record)

    def get_source_normalization(
        self, normalization_id: str
    ) -> LoudspeakerSourceNormalization | None:
        return self.source_normalizations.get(normalization_id)

    def save_drive_condition(self, record: ReferenceDriveCondition) -> None:
        self.drive_conditions.save(record)

    def get_drive_condition(
        self, condition_id: str
    ) -> ReferenceDriveCondition | None:
        return self.drive_conditions.get(condition_id)

    def save_output_anchor(
        self, record: AbsoluteAcousticOutputAnchor
    ) -> None:
        self.output_anchors.save(record)

    def get_output_anchor(
        self, anchor_id: str
    ) -> AbsoluteAcousticOutputAnchor | None:
        return self.output_anchors.get(anchor_id)

    def save_sustained_test(self, record: SustainedOutputTest) -> None:
        self.sustained_tests.save(record)

    def get_sustained_test(
        self, test_id: str
    ) -> SustainedOutputTest | None:
        return self.sustained_tests.get(test_id)

    def save_compression_observation(
        self, record: ThermalCompressionObservation
    ) -> None:
        self.compression_observations.save(record)

    def get_compression_observation(
        self, observation_id: str
    ) -> ThermalCompressionObservation | None:
        return self.compression_observations.get(observation_id)

    def save_recovery_profile(self, record: RecoveryProfile) -> None:
        self.recovery_profiles.save(record)

    def get_recovery_profile(
        self, profile_id: str
    ) -> RecoveryProfile | None:
        return self.recovery_profiles.get(profile_id)

    def save_mic_profile(
        self, record: MeasurementMicrophoneDirectionalProfile
    ) -> None:
        self.mic_profiles.save(record)

    def get_mic_profile(
        self, profile_id: str
    ) -> MeasurementMicrophoneDirectionalProfile | None:
        return self.mic_profiles.get(profile_id)

    def save_orientation_state(
        self, record: ReceiverOrientationState
    ) -> None:
        self.orientation_states.save(record)

    def get_orientation_state(
        self, state_id: str
    ) -> ReceiverOrientationState | None:
        return self.orientation_states.get(state_id)

    def save_incidence_applicability(
        self, record: MicrophoneIncidenceApplicability
    ) -> None:
        self.incidence_applicability.save(record)

    def get_incidence_applicability(
        self, applicability_id: str
    ) -> MicrophoneIncidenceApplicability | None:
        return self.incidence_applicability.get(applicability_id)

    def save_channel_array(self, record: SameChannelSpeakerArray) -> None:
        self.channel_arrays.save(record)

    def get_channel_array(
        self, array_id: str
    ) -> SameChannelSpeakerArray | None:
        return self.channel_arrays.get(array_id)

    def save_reproduction_mode(
        self, record: ArrayReproductionMode
    ) -> None:
        self.reproduction_modes.save(record)

    def get_reproduction_mode(
        self, mode_id: str
    ) -> ArrayReproductionMode | None:
        return self.reproduction_modes.get(mode_id)

    def save_array_qualification(
        self, record: ArrayAcousticQualification
    ) -> None:
        self.array_qualifications.save(record)

    def get_array_qualification(
        self, qualification_id: str
    ) -> ArrayAcousticQualification | None:
        return self.array_qualifications.get(qualification_id)

    def save_front_layer(self, record: LoudspeakerFrontLayer) -> None:
        self.front_layers.save(record)

    def get_front_layer(
        self, layer_id: str
    ) -> LoudspeakerFrontLayer | None:
        return self.front_layers.get(layer_id)

    def save_grille_transfer(self, record: GrilleTransferEvidence) -> None:
        self.grille_transfers.save(record)

    def get_grille_transfer(
        self, evidence_id: str
    ) -> GrilleTransferEvidence | None:
        return self.grille_transfers.get(evidence_id)

    def save_front_layer_applicability(
        self, record: FrontLayerApplicability
    ) -> None:
        self.front_layer_applicability.save(record)

    def get_front_layer_applicability(
        self, applicability_id: str
    ) -> FrontLayerApplicability | None:
        return self.front_layer_applicability.get(applicability_id)
