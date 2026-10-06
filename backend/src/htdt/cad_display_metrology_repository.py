"""Append-only persistence for the REV58-DISPLAYMEAS authorities
(#682 / #680 / #686 / #647 / #666).

Twenty-six tables across five repositories:

* :class:`CadPatternGeneratorFidelityRepository` — #682 stimulus fidelity
* :class:`CadMeterMatchRepository` — #680 probe matching / spectral
  mismatch
* :class:`CadDisplayAdditivityRepository` — #686 additivity / RGB
  separation / volumetric characterisation
* :class:`CadTemporalDisplayRepository` — #647 temporal behaviour
* :class:`CadLutClosedLoopRepository` — #666 LUT closed-loop calibration
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_pattern_generator_fidelity import (
    DeliveredStimulusObservation,
    GeneratorFidelityQualification,
    PatternGeneratorInstance,
    RequestedVideoPatch,
)
from .cad_meter_match import (
    DisplayMeterMatchProfile,
    MeterCorrectionApplicability,
    ProbeMatchObservation,
    ProbeMatchVerification,
)
from .cad_display_additivity import (
    CalibrationModelEligibility,
    CharacterisationPlan,
    DisplayAdditivityObservation,
    HoldoutVerification,
    RGBSeparationAssessment,
    VolumetricCharacterisation,
)
from .cad_temporal_display import (
    FlickerMeasurement,
    ImageRetentionObservation,
    MotionArtifactMeasurement,
    TemporalDisplayQualification,
    TemporalDisplayState,
    TemporalStepResponseMeasurement,
)
from .cad_lut_closed_loop import (
    DisplayLUTArtifact,
    LUTDeploymentRecord,
    LUTGenerationRecord,
    LUTLoopQualification,
    LUTPostVerification,
    LUTPreflightVerification,
)


class DisplayMetrologyConflictError(ValueError):
    """A display-metrology save violated append-only identity rules."""


class DisplayMetrologyIntegrityError(ValueError):
    """A stored display-metrology row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DisplayMetrologyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DisplayMetrologyIntegrityError(
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
        # (column_name, payload path) — path may be dotted for nested
        # refs (``patch_ref.ref_id``); ``__len__`` selects len(record).
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
            raise DisplayMetrologyConflictError(
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
            raise DisplayMetrologyIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise DisplayMetrologyIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise DisplayMetrologyIntegrityError(
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


class CadPatternGeneratorFidelityRepository:
    """Native storage for the #682 pattern-generator fidelity authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_pg_generator_instances',
                'cad_pg_requested_patches',
                'cad_pg_delivered_observations',
                'cad_pg_fidelity_qualifications',
            )
        self.generators = _SealedStore(
            self._connect, 'cad_pg_generator_instances',
            PatternGeneratorInstance, 'generator_id',
            'generator_sha256',
            (
                ('document_id', '__document_id__'),
                ('generator_class', 'generator_class'),
                ('manufacturer', 'manufacturer'),
                ('model', 'model'),
            ),
        )
        self.patches = _SealedStore(
            self._connect, 'cad_pg_requested_patches',
            RequestedVideoPatch, 'patch_id', 'patch_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('stimulus_ref_id', 'stimulus_ref'),
            ),
        )
        self.observations = _SealedStore(
            self._connect, 'cad_pg_delivered_observations',
            DeliveredStimulusObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('patch_ref_id', 'patch_ref'),
                _ref('generator_ref_id', 'generator_ref'),
                ('observation_point', 'observation_point'),
                ('verification', 'verification'),
            ),
        )
        self.qualifications = _SealedStore(
            self._connect, 'cad_pg_fidelity_qualifications',
            GeneratorFidelityQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('generator_ref_id', 'generator_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_generator(self, record: PatternGeneratorInstance) -> None:
        self.generators.save(record)

    def get_generator(
        self, generator_id: str
    ) -> PatternGeneratorInstance | None:
        return self.generators.get(generator_id)

    def save_patch(self, record: RequestedVideoPatch) -> None:
        self.patches.save(record)

    def get_patch(self, patch_id: str) -> RequestedVideoPatch | None:
        return self.patches.get(patch_id)

    def save_observation(
        self, record: DeliveredStimulusObservation
    ) -> None:
        self.observations.save(record)

    def get_observation(
        self, observation_id: str
    ) -> DeliveredStimulusObservation | None:
        return self.observations.get(observation_id)

    def save_qualification(
        self, record: GeneratorFidelityQualification
    ) -> None:
        self.qualifications.save(record)

    def get_qualification(
        self, qualification_id: str
    ) -> GeneratorFidelityQualification | None:
        return self.qualifications.get(qualification_id)


class CadMeterMatchRepository:
    """Native storage for the #680 probe-matching authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_mm_match_profiles',
                'cad_mm_match_observations',
                'cad_mm_verifications',
                'cad_mm_applicability',
            )
        self.profiles = _SealedStore(
            self._connect, 'cad_mm_match_profiles',
            DisplayMeterMatchProfile, 'match_id', 'match_sha256',
            (
                ('document_id', '__document_id__'),
                ('target_serial', 'target_instrument.serial'),
                ('reference_serial', 'reference_instrument.serial'),
                ('display_instance', 'display_state.display_instance'),
            ),
        )
        self.observations = _SealedStore(
            self._connect, 'cad_mm_match_observations',
            ProbeMatchObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('match_ref_id', 'match_ref'),
            ),
        )
        self.verifications = _SealedStore(
            self._connect, 'cad_mm_verifications',
            ProbeMatchVerification, 'verification_id',
            'verification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('match_ref_id', 'match_ref'),
                ('passed', 'passed'),
            ),
        )
        self.applicability = _SealedStore(
            self._connect, 'cad_mm_applicability',
            MeterCorrectionApplicability, 'applicability_id',
            'applicability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('match_ref_id', 'match_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_profile(self, record: DisplayMeterMatchProfile) -> None:
        self.profiles.save(record)

    def get_profile(
        self, match_id: str
    ) -> DisplayMeterMatchProfile | None:
        return self.profiles.get(match_id)

    def save_observation(self, record: ProbeMatchObservation) -> None:
        self.observations.save(record)

    def get_observation(
        self, observation_id: str
    ) -> ProbeMatchObservation | None:
        return self.observations.get(observation_id)

    def save_verification(
        self, record: ProbeMatchVerification
    ) -> None:
        self.verifications.save(record)

    def get_verification(
        self, verification_id: str
    ) -> ProbeMatchVerification | None:
        return self.verifications.get(verification_id)

    def save_applicability(
        self, record: MeterCorrectionApplicability
    ) -> None:
        self.applicability.save(record)

    def get_applicability(
        self, applicability_id: str
    ) -> MeterCorrectionApplicability | None:
        return self.applicability.get(applicability_id)


class CadDisplayAdditivityRepository:
    """Native storage for the #686 additivity/separation authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_da_additivity_observations',
                'cad_da_separation_assessments',
                'cad_da_volumetric_characterisations',
                'cad_da_holdout_verifications',
                'cad_da_model_eligibility',
                'cad_da_characterisation_plans',
            )
        self.observations = _SealedStore(
            self._connect, 'cad_da_additivity_observations',
            DisplayAdditivityObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
            ),
        )
        self.separations = _SealedStore(
            self._connect, 'cad_da_separation_assessments',
            RGBSeparationAssessment, 'assessment_id',
            'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
            ),
        )
        self.characterisations = _SealedStore(
            self._connect, 'cad_da_volumetric_characterisations',
            VolumetricCharacterisation, 'characterisation_id',
            'characterisation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
                ('grid_size', 'grid_size'),
            ),
        )
        self.holdouts = _SealedStore(
            self._connect, 'cad_da_holdout_verifications',
            HoldoutVerification, 'verification_id',
            'verification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
                ('model_family', 'model_family'),
                ('passed', 'passed'),
            ),
        )
        self.eligibility = _SealedStore(
            self._connect, 'cad_da_model_eligibility',
            CalibrationModelEligibility, 'eligibility_id',
            'eligibility_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
                ('model_family', 'model_family'),
                ('verdict', 'verdict'),
            ),
        )
        self.plans = _SealedStore(
            self._connect, 'cad_da_characterisation_plans',
            CharacterisationPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
                ('required_capability', 'required_capability'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_observation(
        self, record: DisplayAdditivityObservation
    ) -> None:
        self.observations.save(record)

    def get_observation(
        self, observation_id: str
    ) -> DisplayAdditivityObservation | None:
        return self.observations.get(observation_id)

    def save_separation(
        self, record: RGBSeparationAssessment
    ) -> None:
        self.separations.save(record)

    def get_separation(
        self, assessment_id: str
    ) -> RGBSeparationAssessment | None:
        return self.separations.get(assessment_id)

    def save_characterisation(
        self, record: VolumetricCharacterisation
    ) -> None:
        self.characterisations.save(record)

    def get_characterisation(
        self, characterisation_id: str
    ) -> VolumetricCharacterisation | None:
        return self.characterisations.get(characterisation_id)

    def save_holdout(self, record: HoldoutVerification) -> None:
        self.holdouts.save(record)

    def get_holdout(
        self, verification_id: str
    ) -> HoldoutVerification | None:
        return self.holdouts.get(verification_id)

    def save_eligibility(
        self, record: CalibrationModelEligibility
    ) -> None:
        self.eligibility.save(record)

    def get_eligibility(
        self, eligibility_id: str
    ) -> CalibrationModelEligibility | None:
        return self.eligibility.get(eligibility_id)

    def save_plan(self, record: CharacterisationPlan) -> None:
        self.plans.save(record)

    def get_plan(
        self, plan_id: str
    ) -> CharacterisationPlan | None:
        return self.plans.get(plan_id)


class CadTemporalDisplayRepository:
    """Native storage for the #647 temporal display authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_td_states',
                'cad_td_step_responses',
                'cad_td_motion_measurements',
                'cad_td_flicker_measurements',
                'cad_td_retention_observations',
                'cad_td_qualifications',
            )
        self.states = _SealedStore(
            self._connect, 'cad_td_states',
            TemporalDisplayState, 'state_id', 'state_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('display_state_ref_id', 'display_state_ref'),
                ('input_frame_rate_hz', 'input_frame_rate_hz'),
                ('refresh_rate_hz', 'refresh_rate_hz'),
            ),
        )
        self.step_responses = _SealedStore(
            self._connect, 'cad_td_step_responses',
            TemporalStepResponseMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('state_ref_id', 'state_ref'),
            ),
        )
        self.motion = _SealedStore(
            self._connect, 'cad_td_motion_measurements',
            MotionArtifactMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('state_ref_id', 'state_ref'),
                ('mechanism', 'mechanism'),
            ),
        )
        self.flicker = _SealedStore(
            self._connect, 'cad_td_flicker_measurements',
            FlickerMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('state_ref_id', 'state_ref'),
                ('method', 'method'),
            ),
        )
        self.retention = _SealedStore(
            self._connect, 'cad_td_retention_observations',
            ImageRetentionObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('state_ref_id', 'state_ref'),
                ('persistence', 'persistence'),
            ),
        )
        self.qualifications = _SealedStore(
            self._connect, 'cad_td_qualifications',
            TemporalDisplayQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('state_ref_id', 'state_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_state(self, record: TemporalDisplayState) -> None:
        self.states.save(record)

    def get_state(self, state_id: str) -> TemporalDisplayState | None:
        return self.states.get(state_id)

    def save_step_response(
        self, record: TemporalStepResponseMeasurement
    ) -> None:
        self.step_responses.save(record)

    def get_step_response(
        self, measurement_id: str
    ) -> TemporalStepResponseMeasurement | None:
        return self.step_responses.get(measurement_id)

    def save_motion_measurement(
        self, record: MotionArtifactMeasurement
    ) -> None:
        self.motion.save(record)

    def get_motion_measurement(
        self, measurement_id: str
    ) -> MotionArtifactMeasurement | None:
        return self.motion.get(measurement_id)

    def save_flicker_measurement(
        self, record: FlickerMeasurement
    ) -> None:
        self.flicker.save(record)

    def get_flicker_measurement(
        self, measurement_id: str
    ) -> FlickerMeasurement | None:
        return self.flicker.get(measurement_id)

    def save_retention_observation(
        self, record: ImageRetentionObservation
    ) -> None:
        self.retention.save(record)

    def get_retention_observation(
        self, observation_id: str
    ) -> ImageRetentionObservation | None:
        return self.retention.get(observation_id)

    def save_qualification(
        self, record: TemporalDisplayQualification
    ) -> None:
        self.qualifications.save(record)

    def get_qualification(
        self, qualification_id: str
    ) -> TemporalDisplayQualification | None:
        return self.qualifications.get(qualification_id)


class CadLutClosedLoopRepository:
    """Native storage for the #666 LUT closed-loop authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_lut_artifacts',
                'cad_lut_generation_records',
                'cad_lut_preflight_verifications',
                'cad_lut_deployments',
                'cad_lut_post_verifications',
                'cad_lut_qualifications',
            )
        self.artifacts = _SealedStore(
            self._connect, 'cad_lut_artifacts',
            DisplayLUTArtifact, 'artifact_id', 'artifact_sha256',
            (
                ('document_id', '__document_id__'),
                ('kind', 'kind'),
            ),
        )
        self.generation = _SealedStore(
            self._connect, 'cad_lut_generation_records',
            LUTGenerationRecord, 'generation_id', 'generation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
            ),
        )
        self.preflight = _SealedStore(
            self._connect, 'cad_lut_preflight_verifications',
            LUTPreflightVerification, 'preflight_id',
            'preflight_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
                (
                    'numeric_validation_passed',
                    'numeric_validation_passed',
                ),
            ),
        )
        self.deployments = _SealedStore(
            self._connect, 'cad_lut_deployments',
            LUTDeploymentRecord, 'deployment_id', 'deployment_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
                ('device_instance', 'device_instance'),
                ('slot', 'slot'),
            ),
        )
        self.post_verifications = _SealedStore(
            self._connect, 'cad_lut_post_verifications',
            LUTPostVerification, 'post_verification_id',
            'post_verification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('passed', 'passed'),
            ),
        )
        self.qualifications = _SealedStore(
            self._connect, 'cad_lut_qualifications',
            LUTLoopQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_artifact(self, record: DisplayLUTArtifact) -> None:
        self.artifacts.save(record)

    def get_artifact(
        self, artifact_id: str
    ) -> DisplayLUTArtifact | None:
        return self.artifacts.get(artifact_id)

    def save_generation(self, record: LUTGenerationRecord) -> None:
        self.generation.save(record)

    def get_generation(
        self, generation_id: str
    ) -> LUTGenerationRecord | None:
        return self.generation.get(generation_id)

    def save_preflight(
        self, record: LUTPreflightVerification
    ) -> None:
        self.preflight.save(record)

    def get_preflight(
        self, preflight_id: str
    ) -> LUTPreflightVerification | None:
        return self.preflight.get(preflight_id)

    def save_deployment(self, record: LUTDeploymentRecord) -> None:
        self.deployments.save(record)

    def get_deployment(
        self, deployment_id: str
    ) -> LUTDeploymentRecord | None:
        return self.deployments.get(deployment_id)

    def save_post_verification(
        self, record: LUTPostVerification
    ) -> None:
        self.post_verifications.save(record)

    def get_post_verification(
        self, post_verification_id: str
    ) -> LUTPostVerification | None:
        return self.post_verifications.get(post_verification_id)

    def save_qualification(
        self, record: LUTLoopQualification
    ) -> None:
        self.qualifications.save(record)

    def get_qualification(
        self, qualification_id: str
    ) -> LUTLoopQualification | None:
        return self.qualifications.get(qualification_id)
