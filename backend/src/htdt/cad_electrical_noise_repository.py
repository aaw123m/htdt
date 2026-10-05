"""Append-only persistence for the hum/buzz/grounding-EMC diagnostic
authority (#606).

Six tables:

* ``cad_electrical_noise_observations`` — sealed noise captures with
  instrument/state context.
* ``cad_audio_interconnects`` — sealed per-link shield/interface
  evidence (composes with the #597 physical-interconnect authority).
* ``cad_noise_isolation_tests`` — sealed bounded divide-and-isolate
  diagnostic runs bound to a stored observation.
* ``cad_humbuzz_diagnostics`` — sealed hypotheses/classifications bound
  to stored observations.
* ``cad_noise_mitigations`` — sealed safe-mitigation attempts; a
  mitigation may only be persisted against a stored diagnostic, and an
  evaluated outcome requires the repeat observation to be stored first.
* ``cad_humbuzz_verdicts`` — sealed per-diagnostic verdicts produced by
  ``evaluate_humbuzz``.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_electrical_noise import (
    AudioInterconnectEvidence,
    ElectricalNoiseObservation,
    HumBuzzDiagnostic,
    HumBuzzVerdict,
    NoiseIsolationTest,
    NoiseMitigationAttempt,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class ElectricalNoiseConflictError(ValueError):
    """An electrical-noise save violated append-only identity rules."""


class ElectricalNoiseIntegrityError(ValueError):
    """A stored electrical-noise row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ElectricalNoiseIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ElectricalNoiseIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadElectricalNoiseRepository:
    """Native storage for the #606 hum/buzz/EMC diagnostic records."""

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
                'cad_electrical_noise_observations',
                'cad_audio_interconnects',
                'cad_noise_isolation_tests',
                'cad_humbuzz_diagnostics',
                'cad_noise_mitigations',
                'cad_humbuzz_verdicts',
            )

    # ------------------------------------------------------------------
    # Shared helpers

    def _get_payload(self, table: str, column: str, key: str, model):
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} '
                f'WHERE {column}=?',
                (key,),
            ).fetchone()
        if row is None:
            return None
        return model.model_validate_json(row['payload_json'])

    def _list(
        self,
        *,
        table: str,
        model,
        where: str = 'document_id=?',
        params: tuple[object, ...] = (),
        order: str = 'seq ASC',
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                f'ORDER BY {order}',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Noise observations

    def save_observation(
        self, observation: ElectricalNoiseObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == (
                observation.observation_sha256
            ):
                return
            raise ElectricalNoiseConflictError(
                'noise observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_electrical_noise_observations (
                    observation_id, observation_sha256, document_id,
                    symptom, instrument, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.symptom,
                    observation.instrument,
                    observation.captured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> ElectricalNoiseObservation | None:
        return self._get_payload(
            'cad_electrical_noise_observations', 'observation_id',
            observation_id, ElectricalNoiseObservation,
        )

    def list_observations(
        self, document_id: str
    ) -> tuple[ElectricalNoiseObservation, ...]:
        return self._list(
            table='cad_electrical_noise_observations',
            model=ElectricalNoiseObservation,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Interconnect evidence

    def save_interconnect(
        self, interconnect: AudioInterconnectEvidence
    ) -> None:
        _assert_sealed(
            interconnect, 'interconnect_sha256', 'interconnect_id'
        )
        existing = self.get_interconnect(interconnect.interconnect_id)
        if existing is not None:
            if existing.interconnect_sha256 == (
                interconnect.interconnect_sha256
            ):
                return
            raise ElectricalNoiseConflictError(
                'interconnect evidence records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_audio_interconnects (
                    interconnect_id, interconnect_sha256, document_id,
                    label, interface_class, shield_termination,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interconnect.interconnect_id,
                    interconnect.interconnect_sha256,
                    interconnect.document_id,
                    interconnect.label,
                    interconnect.interface_class,
                    interconnect.shield_termination,
                    interconnect.declared_at_utc,
                    interconnect.model_dump_json(),
                ),
            )

    def get_interconnect(
        self, interconnect_id: str
    ) -> AudioInterconnectEvidence | None:
        return self._get_payload(
            'cad_audio_interconnects', 'interconnect_id',
            interconnect_id, AudioInterconnectEvidence,
        )

    def list_interconnects(
        self, document_id: str
    ) -> tuple[AudioInterconnectEvidence, ...]:
        return self._list(
            table='cad_audio_interconnects',
            model=AudioInterconnectEvidence,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Isolation tests

    def save_isolation_test(self, test: NoiseIsolationTest) -> None:
        _assert_sealed(test, 'test_sha256', 'test_id')
        existing = self.get_isolation_test(test.test_id)
        if existing is not None:
            if existing.test_sha256 == test.test_sha256:
                return
            raise ElectricalNoiseConflictError(
                'isolation tests are append-only'
            )
        observation = self.get_observation(
            test.observation_ref.ref_id
        )
        if observation is None:
            raise ElectricalNoiseIntegrityError(
                'an isolation test must reference a persisted '
                'observation'
            )
        if observation.observation_sha256 != (
            test.observation_ref.ref_sha256
        ):
            raise ElectricalNoiseIntegrityError(
                'isolation test observation hash does not match the '
                'stored observation'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_noise_isolation_tests (
                    test_id, test_sha256, document_id,
                    observation_ref_id, performed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    test.test_id,
                    test.test_sha256,
                    test.document_id,
                    test.observation_ref.ref_id,
                    test.performed_at_utc,
                    test.model_dump_json(),
                ),
            )

    def get_isolation_test(
        self, test_id: str
    ) -> NoiseIsolationTest | None:
        return self._get_payload(
            'cad_noise_isolation_tests', 'test_id', test_id,
            NoiseIsolationTest,
        )

    def list_isolation_tests(
        self, document_id: str
    ) -> tuple[NoiseIsolationTest, ...]:
        return self._list(
            table='cad_noise_isolation_tests',
            model=NoiseIsolationTest,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Diagnostics

    def save_diagnostic(self, diagnostic: HumBuzzDiagnostic) -> None:
        _assert_sealed(
            diagnostic, 'diagnostic_sha256', 'diagnostic_id'
        )
        existing = self.get_diagnostic(diagnostic.diagnostic_id)
        if existing is not None:
            if existing.diagnostic_sha256 == diagnostic.diagnostic_sha256:
                return
            raise ElectricalNoiseConflictError(
                'diagnostics are append-only'
            )
        for observation_id in diagnostic.observation_refs:
            if self.get_observation(observation_id) is None:
                raise ElectricalNoiseIntegrityError(
                    'a diagnostic must reference persisted observations'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_humbuzz_diagnostics (
                    diagnostic_id, diagnostic_sha256, document_id,
                    classification, hypothesis_state, recommendation,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnostic.diagnostic_id,
                    diagnostic.diagnostic_sha256,
                    diagnostic.document_id,
                    diagnostic.classification,
                    diagnostic.hypothesis_state,
                    diagnostic.recommendation,
                    diagnostic.created_at_utc,
                    diagnostic.model_dump_json(),
                ),
            )

    def get_diagnostic(
        self, diagnostic_id: str
    ) -> HumBuzzDiagnostic | None:
        return self._get_payload(
            'cad_humbuzz_diagnostics', 'diagnostic_id', diagnostic_id,
            HumBuzzDiagnostic,
        )

    def list_diagnostics(
        self, document_id: str
    ) -> tuple[HumBuzzDiagnostic, ...]:
        return self._list(
            table='cad_humbuzz_diagnostics',
            model=HumBuzzDiagnostic,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Mitigation attempts

    def save_mitigation(self, attempt: NoiseMitigationAttempt) -> None:
        _assert_sealed(attempt, 'attempt_sha256', 'attempt_id')
        existing = self.get_mitigation(attempt.attempt_id)
        if existing is not None:
            if existing.attempt_sha256 == attempt.attempt_sha256:
                return
            raise ElectricalNoiseConflictError(
                'mitigation attempts are append-only'
            )
        if self.get_diagnostic(attempt.diagnostic_ref) is None:
            raise ElectricalNoiseIntegrityError(
                'a mitigation must reference a persisted diagnostic'
            )
        if self.get_observation(attempt.before_ref) is None:
            raise ElectricalNoiseIntegrityError(
                'a mitigation must reference its persisted baseline '
                'observation'
            )
        if attempt.after_ref is not None and (
            self.get_observation(attempt.after_ref) is None
        ):
            raise ElectricalNoiseIntegrityError(
                'a mitigation repeat observation must be persisted '
                'first — an unmeasured fix is not a fix'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_noise_mitigations (
                    attempt_id, attempt_sha256, document_id,
                    diagnostic_ref, kind, outcome, performed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    attempt.attempt_sha256,
                    attempt.document_id,
                    attempt.diagnostic_ref,
                    attempt.kind,
                    attempt.outcome,
                    attempt.performed_at_utc,
                    attempt.model_dump_json(),
                ),
            )

    def get_mitigation(
        self, attempt_id: str
    ) -> NoiseMitigationAttempt | None:
        return self._get_payload(
            'cad_noise_mitigations', 'attempt_id', attempt_id,
            NoiseMitigationAttempt,
        )

    def mitigations_for_diagnostic(
        self, diagnostic_id: str
    ) -> tuple[NoiseMitigationAttempt, ...]:
        return self._list(
            table='cad_noise_mitigations',
            model=NoiseMitigationAttempt,
            where='diagnostic_ref=?',
            params=(diagnostic_id,),
        )

    # ------------------------------------------------------------------
    # Verdicts

    def save_verdict(self, verdict: HumBuzzVerdict) -> None:
        _assert_sealed(verdict, 'verdict_sha256', 'verdict_id')
        existing = self.get_verdict(verdict.verdict_id)
        if existing is not None:
            if existing.verdict_sha256 == verdict.verdict_sha256:
                return
            raise ElectricalNoiseConflictError(
                'hum/buzz verdicts are append-only'
            )
        if self.get_diagnostic(verdict.diagnostic_ref) is None:
            raise ElectricalNoiseIntegrityError(
                'a verdict must reference a persisted diagnostic'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_humbuzz_verdicts (
                    verdict_id, verdict_sha256, document_id,
                    diagnostic_ref, state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.verdict_id,
                    verdict.verdict_sha256,
                    verdict.document_id,
                    verdict.diagnostic_ref,
                    verdict.state,
                    verdict.evaluated_at_utc,
                    verdict.model_dump_json(),
                ),
            )

    def get_verdict(self, verdict_id: str) -> HumBuzzVerdict | None:
        return self._get_payload(
            'cad_humbuzz_verdicts', 'verdict_id', verdict_id,
            HumBuzzVerdict,
        )

    def list_verdicts(
        self, document_id: str
    ) -> tuple[HumBuzzVerdict, ...]:
        return self._list(
            table='cad_humbuzz_verdicts',
            model=HumBuzzVerdict,
            params=(document_id,),
        )
