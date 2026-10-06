"""Append-only persistence for the observer-metamerism authority (#626).

Five tables:

* ``cad_om_spectral_states`` — sealed ``DisplaySpectralState`` records
  keyed by ``spectral_state_id``.
* ``cad_om_observer_profiles`` — sealed ``ObserverModelProfile``
  calculation profiles keyed by ``profile_id``.
* ``cad_om_evaluations`` — sealed ``ObserverMetamerismEvaluation`` pair
  results; both bound spectral states and the profile must persist.
* ``cad_om_perceptual_matches`` — sealed ``PerceptualMatchRecord``
  controlled-match records; both bound spectral states must persist.
* ``cad_om_qualifications`` — sealed ``ObserverMetamerismQualification``
  verdicts; both bound spectral states must persist, and a declared
  evaluation must exist.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict.  Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_observer_metamerism import (
    DisplaySpectralState,
    ObserverMetamerismEvaluation,
    ObserverMetamerismQualification,
    ObserverModelProfile,
    PerceptualMatchRecord,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class ObserverMetamerismConflictError(ValueError):
    """An observer-metamerism save violated append-only identity rules."""


class ObserverMetamerismIntegrityError(ValueError):
    """A stored observer-metamerism row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise ObserverMetamerismIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadObserverMetamerismRepository:
    """Native storage for observer-metamerism authority records."""

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
                'cad_om_spectral_states',
                'cad_om_observer_profiles',
                'cad_om_evaluations',
                'cad_om_perceptual_matches',
                'cad_om_qualifications',
            )

    # ------------------------------------------------------------------
    # Spectral states

    def save_spectral_state(self, state: DisplaySpectralState) -> None:
        _assert_sealed(state, 'spectral_state_sha256', 'spectral_state_id')
        existing = self.get_spectral_state(state.spectral_state_id)
        if existing is not None:
            if existing.spectral_state_sha256 == state.spectral_state_sha256:
                return
            raise ObserverMetamerismConflictError(
                'spectral states are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_om_spectral_states (
                    spectral_state_id, spectral_state_sha256, document_id,
                    display_ref, system_kind, evidence_class,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.spectral_state_id,
                    state.spectral_state_sha256,
                    state.document_id,
                    state.display_ref,
                    state.system_kind,
                    state.evidence_class,
                    state.measured_at_utc,
                    state.model_dump_json(),
                ),
            )

    def get_spectral_state(
        self, spectral_state_id: str
    ) -> DisplaySpectralState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT spectral_state_id, spectral_state_sha256,
                       document_id, display_ref, system_kind,
                       evidence_class, measured_at_utc, payload_json
                FROM cad_om_spectral_states
                WHERE spectral_state_id=?
                """,
                (spectral_state_id,),
            ).fetchone()
        if row is None:
            return None
        return self._state_from_row(row)

    def list_spectral_states(
        self, document_id: str
    ) -> tuple[DisplaySpectralState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT spectral_state_id, spectral_state_sha256,
                       document_id, display_ref, system_kind,
                       evidence_class, measured_at_utc, payload_json
                FROM cad_om_spectral_states
                WHERE document_id=?
                ORDER BY display_ref, spectral_state_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._state_from_row(row) for row in rows)

    def _state_from_row(
        self, row: sqlite3.Row
    ) -> DisplaySpectralState:
        state = DisplaySpectralState.model_validate_json(
            row['payload_json']
        )
        if (
            state.spectral_state_id != row['spectral_state_id']
            or state.spectral_state_sha256 != row['spectral_state_sha256']
            or state.document_id != row['document_id']
            or state.display_ref != row['display_ref']
            or state.system_kind != row['system_kind']
            or state.evidence_class != row['evidence_class']
            or state.measured_at_utc != row['measured_at_utc']
        ):
            raise ObserverMetamerismIntegrityError(
                'spectral state row disagrees with its payload'
            )
        return state

    # ------------------------------------------------------------------
    # Observer profiles

    def save_profile(self, profile: ObserverModelProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise ObserverMetamerismConflictError(
                'observer profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_om_observer_profiles (
                    profile_id, profile_sha256, document_id, label,
                    kind, revision, observer_set, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.label,
                    profile.kind,
                    profile.revision,
                    profile.observer_set,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> ObserverModelProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id, label,
                       kind, revision, observer_set, payload_json
                FROM cad_om_observer_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = ObserverModelProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.label != row['label']
            or profile.kind != row['kind']
            or profile.revision != row['revision']
            or profile.observer_set != row['observer_set']
        ):
            raise ObserverMetamerismIntegrityError(
                'observer profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(
        self, evaluation: ObserverMetamerismEvaluation
    ) -> None:
        _assert_sealed(evaluation, 'evaluation_sha256', 'evaluation_id')
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise ObserverMetamerismConflictError(
                'metamerism evaluations are append-only'
            )
        for state_id, role in (
            (evaluation.reference_state_id, 'reference'),
            (evaluation.dut_state_id, 'DUT'),
        ):
            if self.get_spectral_state(state_id) is None:
                raise ObserverMetamerismIntegrityError(
                    f'an evaluation must reference a persisted {role} '
                    'spectral state'
                )
        if self.get_profile(evaluation.profile_id) is None:
            raise ObserverMetamerismIntegrityError(
                'an evaluation must reference a persisted observer profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_om_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    reference_state_id, reference_state_sha256,
                    dut_state_id, dut_state_sha256,
                    profile_id, profile_sha256, result_class,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.reference_state_id,
                    evaluation.reference_state_sha256,
                    evaluation.dut_state_id,
                    evaluation.dut_state_sha256,
                    evaluation.profile_id,
                    evaluation.profile_sha256,
                    evaluation.result_class,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> ObserverMetamerismEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       reference_state_id, reference_state_sha256,
                       dut_state_id, dut_state_sha256,
                       profile_id, profile_sha256, result_class,
                       evaluated_at_utc, payload_json
                FROM cad_om_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def _evaluation_from_row(
        self, row: sqlite3.Row
    ) -> ObserverMetamerismEvaluation:
        evaluation = ObserverMetamerismEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.reference_state_id != row['reference_state_id']
            or evaluation.reference_state_sha256
            != row['reference_state_sha256']
            or evaluation.dut_state_id != row['dut_state_id']
            or evaluation.dut_state_sha256 != row['dut_state_sha256']
            or evaluation.profile_id != row['profile_id']
            or evaluation.profile_sha256 != row['profile_sha256']
            or evaluation.result_class != row['result_class']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ObserverMetamerismIntegrityError(
                'metamerism evaluation row disagrees with its payload'
            )
        return evaluation

    # ------------------------------------------------------------------
    # Perceptual matches

    def save_perceptual_match(
        self, match: PerceptualMatchRecord
    ) -> None:
        _assert_sealed(match, 'match_sha256', 'match_id')
        existing = self.get_perceptual_match(match.match_id)
        if existing is not None:
            if existing.match_sha256 == match.match_sha256:
                return
            raise ObserverMetamerismConflictError(
                'perceptual match records are append-only'
            )
        for state_id in (match.reference_state_id, match.dut_state_id):
            if self.get_spectral_state(state_id) is None:
                raise ObserverMetamerismIntegrityError(
                    'a perceptual match must reference persisted '
                    'spectral states'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_om_perceptual_matches (
                    match_id, match_sha256, document_id,
                    reference_state_id, dut_state_id,
                    observer_identity_class, observer_count,
                    recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match.match_id,
                    match.match_sha256,
                    match.document_id,
                    match.reference_state_id,
                    match.dut_state_id,
                    match.observer_identity_class,
                    match.observer_count,
                    match.recorded_at_utc,
                    match.model_dump_json(),
                ),
            )

    def get_perceptual_match(
        self, match_id: str
    ) -> PerceptualMatchRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT match_id, match_sha256, document_id,
                       reference_state_id, dut_state_id,
                       observer_identity_class, observer_count,
                       recorded_at_utc, payload_json
                FROM cad_om_perceptual_matches
                WHERE match_id=?
                """,
                (match_id,),
            ).fetchone()
        if row is None:
            return None
        match = PerceptualMatchRecord.model_validate_json(
            row['payload_json']
        )
        if (
            match.match_id != row['match_id']
            or match.match_sha256 != row['match_sha256']
            or match.document_id != row['document_id']
            or match.reference_state_id != row['reference_state_id']
            or match.dut_state_id != row['dut_state_id']
            or match.observer_identity_class
            != row['observer_identity_class']
            or match.observer_count != row['observer_count']
            or match.recorded_at_utc != row['recorded_at_utc']
        ):
            raise ObserverMetamerismIntegrityError(
                'perceptual match row disagrees with its payload'
            )
        return match

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: ObserverMetamerismQualification
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
            raise ObserverMetamerismConflictError(
                'qualifications are append-only'
            )
        for state_id in (
            qualification.reference_state_id, qualification.dut_state_id
        ):
            if self.get_spectral_state(state_id) is None:
                raise ObserverMetamerismIntegrityError(
                    'a qualification must reference persisted spectral '
                    'states'
                )
        if (
            qualification.evaluation_id is not None
            and self.get_evaluation(qualification.evaluation_id) is None
        ):
            raise ObserverMetamerismIntegrityError(
                'a bound evaluation must persist'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_om_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    goal, verdict, reference_state_id, dut_state_id,
                    evaluation_id, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.goal,
                    qualification.verdict,
                    qualification.reference_state_id,
                    qualification.dut_state_id,
                    qualification.evaluation_id,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ObserverMetamerismQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, goal, verdict, reference_state_id,
                       dut_state_id, evaluation_id, evaluated_at_utc,
                       payload_json
                FROM cad_om_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = ObserverMetamerismQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.goal != row['goal']
            or qualification.verdict != row['verdict']
            or qualification.reference_state_id
            != row['reference_state_id']
            or qualification.dut_state_id != row['dut_state_id']
            or qualification.evaluation_id != row['evaluation_id']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ObserverMetamerismIntegrityError(
                'qualification row disagrees with its payload'
            )
        return qualification
