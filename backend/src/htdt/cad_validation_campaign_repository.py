from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from .cad_measurement_loop import CadMeasurementPlan
from .cad_measurement_repository import CadMeasurementRepository
from .cad_search import iter_cad_candidate_pages
from .cad_search_repository import CadSearchRepository
from .cad_validation_campaign import (
    CadValidationCampaign,
    CadValidationCampaignRegistration,
    build_validation_campaign_registration,
    exact_campaign_registration,

)

from .cad_schema import require_native_tables, connect_sqlite
from .clock import utc_now_iso as _utc_now


def _aware_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


class CadValidationCampaignRepository:
    """Immutable owned-room O60 preregistration storage.

    The repository commits one campaign together with its durable registration
    under a single `BEGIN IMMEDIATE` write transaction. The registration's
    `registered_at_utc` is repository-generated at commit time and is the only
    timestamp that qualifies as durable preregistration; the caller-supplied
    `CadValidationCampaign.created_at_utc` is identity data, not evidence of
    when the campaign became durable.
    """

    def __init__(
        self,
        search_repository: CadSearchRepository,
        measurement_repository: CadMeasurementRepository,
    ) -> None:
        self.search_repository = search_repository
        self.measurement_repository = measurement_repository
        self.path = Path(search_repository.path)
        if Path(measurement_repository.path) != self.path:
            raise ValueError('campaign repositories must share one native CAD database')
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_validation_campaigns', 'cad_validation_campaign_registrations')

    def _regenerated_candidate_ids(
        self,
        campaign: CadValidationCampaign,
    ) -> frozenset[str]:
        spec = self.search_repository.get(campaign.search_spec_id)
        if spec is None:
            raise ValueError('validation campaign SearchSpec does not exist')
        if spec.document_id != campaign.document_id:
            raise ValueError('validation campaign SearchSpec belongs to another document')
        if spec.search_spec_sha256 != campaign.search_spec_sha256:
            raise ValueError('validation campaign SearchSpec hash mismatch')

        ids: set[str] = set()
        expected_set_sha: str | None = None
        for page in iter_cad_candidate_pages(
            self.search_repository.scene_repository,
            spec,
        ):
            expected_set_sha = page.candidate_set_sha256
            ids.update(candidate.candidate_id for candidate in page.candidates)

        if expected_set_sha is None or expected_set_sha != campaign.candidate_set_sha256:
            raise ValueError('validation campaign candidate-set hash mismatch')
        return frozenset(ids)

    def _preexisting_qualifying_evidence(
        self,
        connection: sqlite3.Connection,
        campaign: CadValidationCampaign,
        candidate_ids: set[str],
    ) -> str | None:
        """Return a description of qualifying evidence that blocks registration.

        Qualifying evidence is candidate-linked measurement state that could
        satisfy the campaign's downstream gates: a measured plan for a
        campaign candidate, measured evidence captured under a candidate's
        applied scene revision, or an existing measurement already claiming
        this campaign through owned-room provenance. The check runs inside the
        same write transaction that inserts the campaign, so a measurement
        cannot commit between the check and the registration.
        """
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('cad_measurement_plans', 'cad_measurements')"
            )
        }
        if 'cad_measurement_plans' in tables:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_plans '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (campaign.search_spec_id,),
            ).fetchall()
            latest: dict[str, CadMeasurementPlan] = {}
            for row in rows:
                plan = CadMeasurementPlan.model_validate_json(row['payload_json'])
                latest[plan.plan_id] = plan
            for plan in latest.values():
                if plan.status == 'measured' and plan.candidate_id in candidate_ids:
                    return (
                        f'measurement plan {plan.plan_id} is already measured '
                        f'for candidate {plan.candidate_id}'
                    )
            applied_revisions = sorted(
                {
                    plan.applied_scene_revision_id
                    for plan in latest.values()
                    if plan.candidate_id in candidate_ids
                }
            )
            if applied_revisions and 'cad_measurements' in tables:
                marks = ', '.join('?' for _ in applied_revisions)
                row = connection.execute(
                    'SELECT measurement_id FROM cad_measurements '
                    "WHERE document_id=? AND evidence_type='measured' "
                    f'AND scene_revision_id IN ({marks}) '
                    'ORDER BY measurement_id LIMIT 1',
                    (campaign.document_id, *applied_revisions),
                ).fetchone()
                if row is not None:
                    return (
                        f"measurement {row['measurement_id']} captured under a "
                        'campaign-candidate applied revision'
                    )
        if 'cad_measurements' in tables:
            rows = connection.execute(
                'SELECT measurement_id, provenance_json FROM cad_measurements '
                'WHERE document_id=?',
                (campaign.document_id,),
            ).fetchall()
            for row in rows:
                try:
                    provenance = json.loads(row['provenance_json'])
                except (TypeError, json.JSONDecodeError):
                    provenance = None
                if (
                    isinstance(provenance, dict)
                    and provenance.get('validation_scope') == 'owned_room'
                    and provenance.get('validation_campaign_id') == campaign.campaign_id
                ):
                    return (
                        f"measurement {row['measurement_id']} already claims this "
                        'campaign through owned-room provenance'
                    )
        return None

    def _commit_registration(
        self,
        connection: sqlite3.Connection,
        campaign: CadValidationCampaign,
        candidate_ids: set[str],
    ) -> CadValidationCampaignRegistration:
        registered_at_utc = _utc_now()
        claimed = _aware_timestamp(campaign.created_at_utc)
        registered = _aware_timestamp(registered_at_utc)
        if claimed is None:
            raise ValueError(
                'validation campaign created_at_utc must be timezone-aware ISO-8601'
            )
        if registered is None:
            raise ValueError(
                'campaign registration registered_at_utc must be timezone-aware ISO-8601'
            )
        if claimed > registered:
            raise ValueError(
                'validation campaign created_at_utc cannot postdate durable registration'
            )
        blocker = self._preexisting_qualifying_evidence(
            connection,
            campaign,
            candidate_ids,
        )
        if blocker is not None:
            raise ValueError(
                'validation campaign cannot register over existing qualifying '
                f'measurement evidence: {blocker}'
            )
        registration = build_validation_campaign_registration(
            campaign=campaign,
            registered_at_utc=registered_at_utc,
        )
        connection.execute(
            '''INSERT INTO cad_validation_campaigns(
                campaign_id, document_id, search_spec_id, model_id, model_version,
                candidate_set_sha256, campaign_sha256, payload_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                campaign.campaign_id,
                campaign.document_id,
                campaign.search_spec_id,
                campaign.model_id,
                campaign.model_version,
                campaign.candidate_set_sha256,
                campaign.campaign_sha256,
                campaign.model_dump_json(),
                registration.registered_at_utc,
            ),
        )
        connection.execute(
            '''INSERT INTO cad_validation_campaign_registrations(
                registration_id, registration_sha256, campaign_id, campaign_sha256,
                registered_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)''',
            (
                registration.registration_id,
                registration.registration_sha256,
                registration.campaign_id,
                registration.campaign_sha256,
                registration.registered_at_utc,
                registration.model_dump_json(),
            ),
        )
        return registration

    def save(
        self,
        campaign: CadValidationCampaign,
    ) -> CadValidationCampaignRegistration:
        """Persist the campaign and its durable registration in one transaction."""
        if not isinstance(campaign, CadValidationCampaign):
            raise TypeError('campaign must be CadValidationCampaign')
        campaign = CadValidationCampaign.model_validate(campaign.model_dump(mode='python'))

        candidate_ids = self._regenerated_candidate_ids(campaign)
        requested = {item.candidate_id for item in campaign.candidates}
        missing = requested - candidate_ids
        if missing:
            raise ValueError(
                f'validation campaign references candidates outside SearchSpec: {sorted(missing)}'
            )

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_campaigns WHERE campaign_id=?',
                (campaign.campaign_id,),
            ).fetchone()
            if row is not None:
                persisted = CadValidationCampaign.model_validate_json(row['payload_json'])
                if persisted != campaign:
                    raise ValueError('validation campaign id has different semantics')
                registration = self._registration_for(connection, campaign.campaign_id)
                if registration is None:
                    # No historical registration time is silently inferred.
                    raise ValueError(
                        'validation campaign registration authority missing/stale'
                    )
                exact_campaign_registration(persisted, registration)
            else:
                registration = self._commit_registration(
                    connection,
                    campaign,
                    requested,
                )
            connection.commit()
        return registration

    @staticmethod
    def _registration_for(
        connection: sqlite3.Connection,
        campaign_id: str,
    ) -> CadValidationCampaignRegistration | None:
        row = connection.execute(
            'SELECT payload_json FROM cad_validation_campaign_registrations '
            'WHERE campaign_id=?',
            (campaign_id,),
        ).fetchone()
        return None if row is None else CadValidationCampaignRegistration.model_validate_json(
            row['payload_json']
        )

    def get_registration(
        self,
        campaign_id: str,
    ) -> CadValidationCampaignRegistration | None:
        with closing(self._connect()) as connection, connection:
            registration = self._registration_for(connection, campaign_id)
        if registration is None:
            return None
        campaign = self.get(campaign_id)
        if campaign is None:
            raise ValueError(
                'validation campaign registration references missing campaign'
            )
        return exact_campaign_registration(campaign, registration)

    def get(self, campaign_id: str) -> CadValidationCampaign | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_campaigns WHERE campaign_id=?',
                (campaign_id,),
            ).fetchone()
        return None if row is None else CadValidationCampaign.model_validate_json(
            row['payload_json']
        )

    def find_by_sha(
        self,
        search_spec_id: str,
        campaign_sha256: str,
    ) -> CadValidationCampaign | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_campaigns '
                'WHERE search_spec_id=? AND campaign_sha256=? ORDER BY seq DESC LIMIT 1',
                (search_spec_id, campaign_sha256),
            ).fetchone()
        return None if row is None else CadValidationCampaign.model_validate_json(
            row['payload_json']
        )

    def list_for_search_spec(
        self,
        search_spec_id: str,
    ) -> tuple[CadValidationCampaign, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_validation_campaigns '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(
            CadValidationCampaign.model_validate_json(row['payload_json'])
            for row in rows
        )

    def count_for_search_spec(self, search_spec_id: str) -> int:
        """Persisted campaign count for one search spec.

        Metadata only — unlike ``list_for_search_spec`` this does not
        deserialize each campaign payload; use it for display counters only.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT COUNT(*) AS campaign_count '
                'FROM cad_validation_campaigns WHERE search_spec_id=?',
                (search_spec_id,),
            ).fetchone()
        return int(row['campaign_count'])
