"""Append-only persistence for the #875 campaign-execution authority
(REV67).

Three sealed stores in one repository:

- ``cad_campaign_execution_plans`` — the sealed executable plans
  (``mcplan-``), one per registered campaign revision;
- ``cad_campaign_execution_events`` — the journal (``mcevt-``), strictly
  ordered by ``seq`` inside a plan, pinned to ``plan_sha256``;
- ``cad_campaign_execution_runs`` — one sealed attempt record
  (``mcrun-``) per capture attempt. Append-only: retries INSERT a new
  attempt record; a failed attempt is evidence forever and can never be
  overwritten.

``runner_for`` rebuilds a :class:`MeasurementCampaignRunner` from the
persisted journal + run records — restart recovery is just "reload the
journal and keep going".
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any, Callable

from .cad_authority_resolver import AuthorityRef
from .cad_campaign_execution import (
    CampaignExecutionPlan,
    CampaignQueueEntry,
)
from .cad_campaign_execution_evidence import (
    CadCampaignExecutionEvent,
    CadCampaignRunRecord,
)
from .cad_campaign_execution_runner import (
    AcquisitionSink,
    ArmConfirmationProvider,
    CalibrationStateProvider,
    EngineFactory,
    MeasurementCampaignRunner,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_sweep_acquisition import (
    AcquisitionResult,
    MeasurementAcquisitionEngine,
)
from .cad_sweep_acquisition_evidence import build_stimulus_definition
from .cad_sweep_acquisition_repository import CadSweepAcquisitionRepository
from .clock import utc_now_iso as _utc_now


class CampaignExecutionConflictError(ValueError):
    """A campaign-execution save violated append-only identity rules."""


class CampaignExecutionIntegrityError(ValueError):
    """A stored campaign-execution row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    from .canonical_json import canonical_sha256

    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CampaignExecutionIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CampaignExecutionIntegrityError(
            'record id does not match its sealed sha256')


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

    def save(self, record: Any, connection: Any | None = None) -> None:
        if connection is not None:
            self._save_in(connection, record)
            return
        with closing(self._connect()) as owned, owned:
            self._save_in(owned, record)

    def _save_in(self, connection: Any, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self._get_in(connection, rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise CampaignExecutionConflictError(
                f'{self.table} records are append-only')
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
        connection.execute(
            f'INSERT INTO {self.table} ({cols}) '
            f'VALUES ({placeholders})',
            values,
        )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            return self._get_in(connection, rid)

    def _get_in(self, connection: Any, rid: str) -> Any | None:
        row = connection.execute(
            f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
            (rid,),
        ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise CampaignExecutionIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise CampaignExecutionIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise CampaignExecutionIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(
        self,
        document_id: str | None = None,
        order_by: str = 'seq',
    ) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        if order_by:
            query += f' ORDER BY {order_by} ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise CampaignExecutionIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCampaignExecutionRepository:
    """Native storage for the #875 campaign-execution authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        sweep_repository: CadSweepAcquisitionRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.sweep_repository = sweep_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_campaign_execution_plans',
                'cad_campaign_execution_events',
                'cad_campaign_execution_runs',
            )
        self.plans = _SealedStore(
            self._connect, 'cad_campaign_execution_plans',
            CampaignExecutionPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                ('authority_version', 'authority_version'),
                _ref('campaign_ref_id', 'campaign_ref'),
                _ref('scene_ref_id', 'scene_ref'),
                ('entry_count', 'entry_count'),
                ('generated_at_utc', 'generated_at_utc'),
            ),
        )
        self.events = _SealedStore(
            self._connect, 'cad_campaign_execution_events',
            CadCampaignExecutionEvent, 'event_id', 'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('plan_id', 'plan_id'),
                ('journal_seq', 'seq'),
                ('kind', 'kind'),
                ('entry_key', 'entry_key'),
                ('position_id', 'position_id'),
                ('actor', 'actor'),
                ('at_utc', 'at_utc'),
                ('retry_decision', 'retry_decision'),
                ('confirmation_method', 'confirmation_method'),
            ),
        )
        self.run_records = _SealedStore(
            self._connect, 'cad_campaign_execution_runs',
            CadCampaignRunRecord, 'run_record_id', 'run_record_sha256',
            (
                ('document_id', '__document_id__'),
                ('plan_id', 'plan_id'),
                ('entry_key', 'entry_key'),
                ('entry_ordinal', 'entry_ordinal'),
                ('attempt', 'attempt'),
                ('position_id', 'position_id'),
                ('channel_entity_id', 'channel_entity_id'),
                ('role', 'role'),
                ('run_index', 'run_index'),
                ('required', 'required'),
                ('level_dbfs', 'level_dbfs'),
                ('outcome', 'outcome'),
                ('failure_kind', 'failure_kind'),
                ('quality_verdict', 'quality_verdict'),
                _ref('stimulus_ref_id', 'stimulus_ref'),
                _ref('acquisition_ref_id', 'acquisition_ref'),
                _ref('position_ref_id', 'position_ref'),
                ('started_at_utc', 'started_at_utc'),
                ('completed_at_utc', 'completed_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # plans -----------------------------------------------------------

    def save_plan(self, plan: CampaignExecutionPlan) -> None:
        self.plans.save(plan)

    def get_plan(self, plan_id: str) -> CampaignExecutionPlan | None:
        return self.plans.get(plan_id)

    def list_plans(
        self, document_id: str | None = None,
    ) -> tuple[CampaignExecutionPlan, ...]:
        return self.plans.list(document_id, order_by='generated_at_utc')

    # journal ---------------------------------------------------------

    def save_event(self, event: CadCampaignExecutionEvent) -> None:
        self.events.save(event)

    def get_event(
        self, event_id: str,
    ) -> CadCampaignExecutionEvent | None:
        return self.events.get(event_id)

    def list_events(
        self, plan_id: str,
    ) -> tuple[CadCampaignExecutionEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_campaign_execution_events '
                'WHERE plan_id=? ORDER BY journal_seq ASC', (plan_id,),
            ).fetchall()
        records = []
        for row in rows:
            record = CadCampaignExecutionEvent.model_validate_json(
                row['payload_json'])
            for column, path in self.events.columns:
                expected = self.events._column_value(record, path)
                if isinstance(expected, bool):
                    expected = int(expected)
                if row[column] != expected:
                    raise CampaignExecutionIntegrityError(
                        'stored event column disagrees with payload')
            records.append(record)
        return tuple(records)

    # run records -----------------------------------------------------

    def save_run_record(self, record: CadCampaignRunRecord) -> None:
        self.run_records.save(record)

    def get_run_record(
        self, run_record_id: str,
    ) -> CadCampaignRunRecord | None:
        return self.run_records.get(run_record_id)

    def list_run_records(
        self, plan_id: str,
    ) -> tuple[CadCampaignRunRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_campaign_execution_runs '
                'WHERE plan_id=? ORDER BY entry_ordinal ASC, attempt ASC',
                (plan_id,),
            ).fetchall()
        records = []
        for row in rows:
            record = CadCampaignRunRecord.model_validate_json(
                row['payload_json'])
            for column, path in self.run_records.columns:
                expected = self.run_records._column_value(record, path)
                if isinstance(expected, bool):
                    expected = int(expected)
                if row[column] != expected:
                    raise CampaignExecutionIntegrityError(
                        'stored run-record column disagrees with payload')
            records.append(record)
        return tuple(records)

    # wiring ------------------------------------------------------------

    def plan_ref(self, plan: CampaignExecutionPlan) -> AuthorityRef:
        return AuthorityRef(
            kind='campaign_execution_plan',
            ref_id=plan.plan_id, ref_sha256=plan.plan_sha256)

    def acquisition_sink(
        self,
        *,
        document_id: str,
        plan: CampaignExecutionPlan,
        position_ref_for: Callable[[str], AuthorityRef | None]
        | None = None,
        orientation_ref_for: Callable[[str], AuthorityRef | None]
        | None = None,
        role_ref_for: Callable[[CampaignQueueEntry], AuthorityRef | None]
        | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> AcquisitionSink:
        """The #869-evidence persister the runner calls per attempt.

        Only engine runs that reached a terminal stage produce an
        acquisition record — precheck/arm failures are honest ``None``
        refs on the campaign run record, not fabricated evidence.
        """

        if self.sweep_repository is None:
            raise CampaignExecutionIntegrityError(
                'acquisition_sink needs a CadSweepAcquisitionRepository')

        def _sink(
            engine: MeasurementAcquisitionEngine,
            result: AcquisitionResult,
            entry: CampaignQueueEntry,
        ) -> tuple[AuthorityRef | None, AuthorityRef | None]:
            stimulus_ref: AuthorityRef | None = None
            if result.stimulus is not None:
                stimulus_record = build_stimulus_definition(
                    document_id=document_id,
                    stimulus=result.stimulus,
                    created_at_utc=clock(),
                )
                self.sweep_repository.save_stimulus_definition(
                    stimulus_record)
                stimulus_ref = AuthorityRef(
                    kind='sweep_stimulus_definition',
                    ref_id=stimulus_record.stimulus_definition_id,
                    ref_sha256=stimulus_record.stimulus_sha256,
                )
            acquisition_ref: AuthorityRef | None = None
            if stimulus_ref is not None:
                role_ref = (
                    role_ref_for(entry) if role_ref_for is not None
                    else None)
                position_ref = (
                    position_ref_for(entry.position_id)
                    if position_ref_for is not None else None)
                orientation_ref = (
                    orientation_ref_for(entry.position_id)
                    if orientation_ref_for is not None else None)
                record = self.sweep_repository.record_run(
                    document_id=document_id,
                    engine=engine, result=result,
                    stimulus_ref=stimulus_ref,
                    campaign_ref=self.plan_ref(plan),
                    role_refs=((role_ref,) if role_ref is not None else ()),
                    position_ref=position_ref,
                    orientation_ref=orientation_ref,
                    notes=(
                        f'campaign plan {plan.plan_id}',
                        f'queue entry {entry.entry_key} role '
                        f'{entry.role} run_index {entry.run_index}',
                    ),
                )
                acquisition_ref = AuthorityRef(
                    kind='sweep_acquisition_run',
                    ref_id=record.acquisition_id,
                    ref_sha256=record.acquisition_sha256,
                )
            return stimulus_ref, acquisition_ref

        return _sink

    def runner_for(
        self,
        plan: CampaignExecutionPlan,
        *,
        engine_factory: EngineFactory,
        arm_confirmation_provider: ArmConfirmationProvider | None = None,
        calibration_state_provider: CalibrationStateProvider | None = None,
        position_ref_for: Callable[[str], AuthorityRef | None]
        | None = None,
        acquisition_sink: AcquisitionSink | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> MeasurementCampaignRunner:
        """Reassemble a runner from the persisted journal — this IS the
        restart-recovery path."""

        if acquisition_sink is None and self.sweep_repository is not None:
            acquisition_sink = self.acquisition_sink(
                document_id=plan.document_id, plan=plan,
                position_ref_for=position_ref_for)
        return MeasurementCampaignRunner(
            plan=plan,
            engine_factory=engine_factory,
            event_sink=self.save_event,
            run_record_sink=self.save_run_record,
            acquisition_sink=acquisition_sink,
            arm_confirmation_provider=arm_confirmation_provider,
            calibration_state_provider=calibration_state_provider,
            position_ref_for=position_ref_for,
            clock=clock,
            events=self.list_events(plan.plan_id),
            run_records=self.list_run_records(plan.plan_id),
        )


__all__ = [
    'CadCampaignExecutionRepository',
    'CampaignExecutionConflictError',
    'CampaignExecutionIntegrityError',
]
