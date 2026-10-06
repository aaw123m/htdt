"""Append-only persistence for the channel-identity / polarity authority
(#621, REV57-AUD).

Five tables:

* ``cad_channel_identity_chains`` — sealed logical→physical chain
  declarations.
* ``cad_acoustic_endpoint_observations`` — sealed endpoint observations.
* ``cad_channel_identity_tests`` — sealed tests binding a chain to an
  exact stimulus and its observations.
* ``cad_polarity_verification_records`` — sealed per-layer polarity
  evidence (wiring / DSP / source / acoustic / phase kept separate).
* ``cad_channel_identity_evaluations`` — sealed per-channel verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_channel_identity_authority import (
    CadAcousticEndpointObservation,
    CadChannelIdentityChain,
    CadChannelIdentityEvaluation,
    CadChannelIdentityTest,
    CadPolarityVerificationRecord,
)


class ChannelIdentityConflictError(ValueError):
    """A channel-identity save violated append-only identity rules."""


class ChannelIdentityIntegrityError(ValueError):
    """A stored channel-identity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ChannelIdentityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ChannelIdentityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadChannelIdentityRepository:
    """Native storage for the #621 channel-identity authority."""

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
                'cad_channel_identity_chains',
                'cad_acoustic_endpoint_observations',
                'cad_channel_identity_tests',
                'cad_polarity_verification_records',
                'cad_channel_identity_evaluations',
            )

    # ------------------------------------------------------------------
    # Chains

    def save_chain(self, chain: CadChannelIdentityChain) -> None:
        _assert_sealed(chain, 'chain_sha256', 'chain_id')
        existing = self.get_chain(chain.chain_id)
        if existing is not None:
            if existing.chain_sha256 == chain.chain_sha256:
                return
            raise ChannelIdentityConflictError(
                'channel identity chains are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_channel_identity_chains (
                    chain_id, chain_sha256, document_id, logical_channel,
                    channel_class, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain.chain_id,
                    chain.chain_sha256,
                    chain.document_id,
                    chain.logical_channel,
                    chain.channel_class,
                    chain.declared_at_utc,
                    chain.model_dump_json(),
                ),
            )

    def get_chain(
        self, chain_id: str
    ) -> CadChannelIdentityChain | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_channel_identity_chains '
                'WHERE chain_id=?',
                (chain_id,),
            ).fetchone()
        if row is None:
            return None
        chain = CadChannelIdentityChain.model_validate_json(
            row['payload_json']
        )
        if (
            chain.chain_id != row['chain_id']
            or chain.chain_sha256 != row['chain_sha256']
            or chain.document_id != row['document_id']
            or chain.logical_channel != row['logical_channel']
            or chain.channel_class != row['channel_class']
            or chain.declared_at_utc != row['declared_at_utc']
        ):
            raise ChannelIdentityIntegrityError(
                'stored chain row disagrees with its payload'
            )
        return chain

    def list_chains(
        self, document_id: str | None = None
    ) -> tuple[CadChannelIdentityChain, ...]:
        query = 'SELECT payload_json FROM cad_channel_identity_chains'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadChannelIdentityChain.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Endpoint observations

    def save_observation(
        self, observation: CadAcousticEndpointObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise ChannelIdentityConflictError(
                'endpoint observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_acoustic_endpoint_observations (
                    observation_id, observation_sha256, document_id,
                    method, confidence, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.method,
                    observation.confidence,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadAcousticEndpointObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_acoustic_endpoint_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadAcousticEndpointObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.method != row['method']
            or observation.confidence != row['confidence']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise ChannelIdentityIntegrityError(
                'stored endpoint observation row disagrees with its payload'
            )
        return observation

    def list_observations(
        self, document_id: str | None = None
    ) -> tuple[CadAcousticEndpointObservation, ...]:
        query = (
            'SELECT payload_json FROM cad_acoustic_endpoint_observations'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadAcousticEndpointObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Identity tests

    def save_test(self, test: CadChannelIdentityTest) -> None:
        _assert_sealed(test, 'test_sha256', 'test_id')
        existing = self.get_test(test.test_id)
        if existing is not None:
            if existing.test_sha256 == test.test_sha256:
                return
            raise ChannelIdentityConflictError(
                'channel identity tests are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_channel_identity_tests (
                    test_id, test_sha256, document_id, chain_ref_id,
                    stimulus_class, tested_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test.test_id,
                    test.test_sha256,
                    test.document_id,
                    test.chain_ref.ref_id,
                    test.stimulus.stimulus_class,
                    test.tested_at_utc,
                    test.model_dump_json(),
                ),
            )

    def get_test(
        self, test_id: str
    ) -> CadChannelIdentityTest | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_channel_identity_tests WHERE test_id=?',
                (test_id,),
            ).fetchone()
        if row is None:
            return None
        test = CadChannelIdentityTest.model_validate_json(
            row['payload_json']
        )
        if (
            test.test_id != row['test_id']
            or test.test_sha256 != row['test_sha256']
            or test.document_id != row['document_id']
            or test.chain_ref.ref_id != row['chain_ref_id']
            or test.stimulus.stimulus_class != row['stimulus_class']
            or test.tested_at_utc != row['tested_at_utc']
        ):
            raise ChannelIdentityIntegrityError(
                'stored identity test row disagrees with its payload'
            )
        return test

    def list_tests(
        self, document_id: str | None = None
    ) -> tuple[CadChannelIdentityTest, ...]:
        query = 'SELECT payload_json FROM cad_channel_identity_tests'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadChannelIdentityTest.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Polarity records

    def save_polarity_record(
        self, record: CadPolarityVerificationRecord
    ) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_polarity_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise ChannelIdentityConflictError(
                'polarity verification records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_polarity_verification_records (
                    record_id, record_sha256, document_id, chain_ref_id,
                    physical_wiring_state, dsp_polarity_state,
                    acoustic_polarity_state, policy_acceptance,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.chain_ref.ref_id,
                    record.layer_state('physical_wiring'),
                    record.layer_state('dsp_inversion'),
                    record.layer_state('acoustic_relative'),
                    record.policy_acceptance,
                    record.measured_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_polarity_record(
        self, record_id: str
    ) -> CadPolarityVerificationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_polarity_verification_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = CadPolarityVerificationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.chain_ref.ref_id != row['chain_ref_id']
            or record.layer_state('physical_wiring')
            != row['physical_wiring_state']
            or record.layer_state('dsp_inversion')
            != row['dsp_polarity_state']
            or record.layer_state('acoustic_relative')
            != row['acoustic_polarity_state']
            or record.policy_acceptance != row['policy_acceptance']
            or record.measured_at_utc != row['measured_at_utc']
        ):
            raise ChannelIdentityIntegrityError(
                'stored polarity record row disagrees with its payload'
            )
        return record

    def list_polarity_records(
        self, document_id: str | None = None
    ) -> tuple[CadPolarityVerificationRecord, ...]:
        query = 'SELECT payload_json FROM cad_polarity_verification_records'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadPolarityVerificationRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(
        self, evaluation: CadChannelIdentityEvaluation
    ) -> None:
        _assert_sealed(
            evaluation, 'evaluation_sha256', 'evaluation_id'
        )
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise ChannelIdentityConflictError(
                'channel identity evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_channel_identity_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    chain_ref_id, logical_channel, verdict,
                    reconciliation, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.chain_ref.ref_id,
                    evaluation.logical_channel,
                    evaluation.verdict,
                    evaluation.reconciliation,
                    evaluation.evaluation_version,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> CadChannelIdentityEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_channel_identity_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = CadChannelIdentityEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.chain_ref.ref_id != row['chain_ref_id']
            or evaluation.logical_channel != row['logical_channel']
            or evaluation.verdict != row['verdict']
            or evaluation.reconciliation != row['reconciliation']
            or evaluation.evaluation_version != row['evaluation_version']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ChannelIdentityIntegrityError(
                'stored evaluation row disagrees with its payload'
            )
        return evaluation

    def list_evaluations(
        self, document_id: str | None = None
    ) -> tuple[CadChannelIdentityEvaluation, ...]:
        query = 'SELECT payload_json FROM cad_channel_identity_evaluations'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadChannelIdentityEvaluation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
