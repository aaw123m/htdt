"""Append-only persistence for the gain-structure / noise-floor /
clipping-margin authority (#651, REV58-MEASELEC).

Four tables:

* ``cad_signal_level_references`` — sealed typed level references
  (dBFS↔analog anchors per stage).
* ``cad_noise_floor_observations`` — sealed per-stage noise-floor
  measurements with class, weighting and analyzer-floor distance.
* ``cad_clipping_margins`` — sealed per-stage clipping-margin records
  with typed mechanism and load-stress scope.
* ``cad_gain_structure_qualifications`` — sealed fail-closed end-to-end
  verdicts for a declared use case.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_gain_noise_structure import (
    CadClippingMarginQualification,
    CadGainStructureQualification,
    CadNoiseFloorObservation,
    CadSignalLevelReference,
)


class GainNoiseAuthorityConflictError(ValueError):
    """A gain-structure save violated append-only identity rules."""


class GainNoiseAuthorityIntegrityError(ValueError):
    """A stored gain-structure row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise GainNoiseAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise GainNoiseAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadGainStructureRepository:
    """Native storage for the #651 gain-structure authority records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_signal_level_references',
                'cad_noise_floor_observations',
                'cad_clipping_margins',
                'cad_gain_structure_qualifications',
            )

    # ------------------------------------------------------------------
    # Signal level references

    def save_level_reference(
        self, reference: CadSignalLevelReference
    ) -> None:
        _assert_sealed(reference, 'reference_sha256', 'reference_id')
        existing = self.get_level_reference(reference.reference_id)
        if existing is not None:
            if existing.reference_sha256 == reference.reference_sha256:
                return
            raise GainNoiseAuthorityConflictError(
                'level references are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_signal_level_references (
                    reference_id, reference_sha256, document_id,
                    stage_label, analog_unit, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference.reference_id,
                    reference.reference_sha256,
                    reference.document_id,
                    reference.stage.stage_label,
                    reference.analog_unit,
                    reference.declared_at_utc,
                    reference.model_dump_json(),
                ),
            )

    def get_level_reference(
        self, reference_id: str
    ) -> CadSignalLevelReference | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_signal_level_references '
                'WHERE reference_id=?',
                (reference_id,),
            ).fetchone()
        if row is None:
            return None
        reference = CadSignalLevelReference.model_validate_json(
            row['payload_json']
        )
        if (
            reference.reference_id != row['reference_id']
            or reference.reference_sha256 != row['reference_sha256']
            or reference.document_id != row['document_id']
            or reference.stage.stage_label != row['stage_label']
            or reference.analog_unit != row['analog_unit']
            or reference.declared_at_utc != row['declared_at_utc']
        ):
            raise GainNoiseAuthorityIntegrityError(
                'level reference row disagrees with payload'
            )
        return reference

    def list_level_references(
        self, document_id: str
    ) -> tuple[CadSignalLevelReference, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_signal_level_references '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSignalLevelReference.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Noise floor observations

    def save_noise_observation(
        self, observation: CadNoiseFloorObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_noise_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise GainNoiseAuthorityConflictError(
                'noise floor observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_noise_floor_observations (
                    observation_id, observation_sha256, document_id,
                    stage_label, noise_class, noise_level, noise_unit,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    (
                        observation.stage.stage_label
                        if observation.stage is not None
                        else None
                    ),
                    observation.noise_class,
                    observation.noise_level,
                    observation.noise_unit,
                    observation.declared_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_noise_observation(
        self, observation_id: str
    ) -> CadNoiseFloorObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_noise_floor_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadNoiseFloorObservation.model_validate_json(
            row['payload_json']
        )
        stage_label = (
            observation.stage.stage_label
            if observation.stage is not None
            else None
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or stage_label != row['stage_label']
            or observation.noise_class != row['noise_class']
            or observation.noise_level != row['noise_level']
            or observation.noise_unit != row['noise_unit']
            or observation.declared_at_utc != row['declared_at_utc']
        ):
            raise GainNoiseAuthorityIntegrityError(
                'noise observation row disagrees with payload'
            )
        return observation

    def list_noise_observations(
        self, document_id: str
    ) -> tuple[CadNoiseFloorObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_noise_floor_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadNoiseFloorObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Clipping margins

    def save_clipping_margin(
        self, margin: CadClippingMarginQualification
    ) -> None:
        _assert_sealed(margin, 'margin_sha256', 'margin_id')
        existing = self.get_clipping_margin(margin.margin_id)
        if existing is not None:
            if existing.margin_sha256 == margin.margin_sha256:
                return
            raise GainNoiseAuthorityConflictError(
                'clipping margins are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_clipping_margins (
                    margin_id, margin_sha256, document_id,
                    stage_label, clip_mechanism, load_stress,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    margin.margin_id,
                    margin.margin_sha256,
                    margin.document_id,
                    margin.stage.stage_label,
                    margin.clip_mechanism,
                    margin.load_stress,
                    margin.declared_at_utc,
                    margin.model_dump_json(),
                ),
            )

    def get_clipping_margin(
        self, margin_id: str
    ) -> CadClippingMarginQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_clipping_margins '
                'WHERE margin_id=?',
                (margin_id,),
            ).fetchone()
        if row is None:
            return None
        margin = CadClippingMarginQualification.model_validate_json(
            row['payload_json']
        )
        if (
            margin.margin_id != row['margin_id']
            or margin.margin_sha256 != row['margin_sha256']
            or margin.document_id != row['document_id']
            or margin.stage.stage_label != row['stage_label']
            or margin.clip_mechanism != row['clip_mechanism']
            or margin.load_stress != row['load_stress']
            or margin.declared_at_utc != row['declared_at_utc']
        ):
            raise GainNoiseAuthorityIntegrityError(
                'clipping margin row disagrees with payload'
            )
        return margin

    def list_clipping_margins(
        self, document_id: str
    ) -> tuple[CadClippingMarginQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_clipping_margins '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadClippingMarginQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Gain-structure qualifications

    def save_qualification(
        self, qualification: CadGainStructureQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise GainNoiseAuthorityConflictError(
                'gain-structure qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_gain_structure_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    use_case, state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.use_case,
                    qualification.state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadGainStructureQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_gain_structure_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadGainStructureQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.use_case != row['use_case']
            or qualification.state != row['state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise GainNoiseAuthorityIntegrityError(
                'gain-structure qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadGainStructureQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_gain_structure_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadGainStructureQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadGainStructureRepository',
    'GainNoiseAuthorityConflictError',
    'GainNoiseAuthorityIntegrityError',
]
