from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.rew_api import RewApiClient, RewApiError

from audit_o60_owned_room import DATABASE_NAME, _build_repositories, _readonly_snapshot


def default_inventory_data_dir() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    return (
        Path(base) / 'HomeTheaterDigitalTwin'
        if base
        else Path.home() / '.home-theater-digital-twin'
    )


def inventory(data_dir: Path) -> dict[str, object]:
    source_database = data_dir / DATABASE_NAME
    report: dict[str, object] = {
        'data_dir': str(data_dir),
        'database_exists': source_database.is_file(),
        'campaigns': [],
        'rew': {},
    }
    if not source_database.is_file():
        return report

    with tempfile.TemporaryDirectory(prefix='htdt-o60r-inventory-') as temp_name:
        snapshot_database = Path(temp_name) / DATABASE_NAME
        _readonly_snapshot(source_database, snapshot_database)
        (
            _scene,
            _search,
            _measurement,
            _roomsim,
            _objective,
            campaign_repository,
            campaign_service,
            validation_repository,
        ) = _build_repositories(snapshot_database)

        with closing(sqlite3.connect(snapshot_database)) as connection:
            campaign_ids = [
                str(row[0])
                for row in connection.execute(
                    'SELECT campaign_id FROM cad_validation_campaigns ORDER BY seq ASC'
                ).fetchall()
            ]

        campaigns: list[dict[str, object]] = []
        for campaign_id in campaign_ids:
            campaign = campaign_repository.get(campaign_id)
            if campaign is None:
                continue
            registration = campaign_repository.get_registration(campaign_id)
            readiness = campaign_service.readiness(campaign_id)
            validations = validation_repository.list_for_search_spec(
                campaign.search_spec_id
            )
            current_eligible = validation_repository.latest_eligible_for_search_spec(
                campaign.search_spec_id
            )
            campaigns.append({
                'campaign_id': campaign.campaign_id,
                'campaign_sha256': campaign.campaign_sha256,
                'created_at_utc': campaign.created_at_utc,
                'registered_at_utc': (
                    None if registration is None else registration.registered_at_utc
                ),
                'registration_id': (
                    None if registration is None else registration.registration_id
                ),
                'document_id': campaign.document_id,
                'search_spec_id': campaign.search_spec_id,
                'search_spec_sha256': campaign.search_spec_sha256,
                'candidate_set_sha256': campaign.candidate_set_sha256,
                'model_id': campaign.model_id,
                'model_version': campaign.model_version,
                'evidence_ready': readiness.evidence_ready,
                'readiness_missing_reasons': list(readiness.missing_reasons),
                'candidates': [
                    {
                        'candidate_id': item.candidate_id,
                        'split': item.split,
                        'measurement_plan_id': item.measurement_plan_id,
                        'measurement_ids': list(item.measurement_ids),
                        'prediction_attempt_id': item.prediction_attempt_id,
                        'predicted_evaluation_id': item.predicted_evaluation_id,
                        'measured_evaluation_id': item.measured_evaluation_id,
                        'missing_reasons': list(item.missing_reasons),
                    }
                    for item in readiness.candidates
                ],
                'validation_records': [
                    {
                        'validation_id': record.validation_id,
                        'recommendation_gate': record.recommendation_gate,
                        'validation_sha256': record.validation_sha256,
                        'created_at_utc': record.created_at_utc,
                    }
                    for record in validations
                    if record.campaign_id == campaign.campaign_id
                ],
                'current_o70_entry_validation_id': (
                    None
                    if current_eligible is None
                    or current_eligible.campaign_id != campaign.campaign_id
                    else current_eligible.validation_id
                ),
            })
        report['campaigns'] = campaigns
    return report


def rew_inventory() -> dict[str, object]:
    client = RewApiClient()
    try:
        measurements = client.list_measurements()
    except RewApiError as exc:
        return {
            'available': False,
            'error': f'{type(exc).__name__}: {exc}',
        }

    result: dict[str, object] = {
        'available': True,
        'measurement_count': len(measurements),
    }
    try:
        result['audio'] = client.get_audio_preflight()
    except RewApiError as exc:
        result['audio'] = None
        result['audio_error'] = f'{type(exc).__name__}: {exc}'
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Read-only inventory for the O60 owned-room validation workflow.'
    )
    parser.add_argument('--data-dir', type=Path, default=default_inventory_data_dir())
    parser.add_argument(
        '--skip-rew',
        action='store_true',
        help='do not query the localhost REW read-only API',
    )
    args = parser.parse_args()

    try:
        report = inventory(args.data_dir.resolve())
        report['rew'] = {} if args.skip_rew else rew_inventory()
    except Exception as exc:
        print(f'O60R_INVENTORY_ERROR={type(exc).__name__}: {exc}', flush=True)
        return 1

    campaigns = report['campaigns']
    assert isinstance(campaigns, list)
    print(f"O60R_DATA_DIR={report['data_dir']}", flush=True)
    print(f"O60R_DATABASE_EXISTS={report['database_exists']}", flush=True)
    print(f'O60R_CAMPAIGN_COUNT={len(campaigns)}', flush=True)
    for campaign in campaigns:
        print(
            'O60R_CAMPAIGN='
            f"{campaign['campaign_id']} "
            f"ready={campaign['evidence_ready']} "
            f"model={campaign['model_id']}/{campaign['model_version']} "
            f"validation={campaign['current_o70_entry_validation_id']}",
            flush=True,
        )
    rew = report['rew']
    if isinstance(rew, dict) and rew:
        print(f"O60R_REW_AVAILABLE={rew.get('available')}", flush=True)
        if rew.get('available'):
            print(
                f"O60R_REW_MEASUREMENT_COUNT={rew.get('measurement_count')}",
                flush=True,
            )
        elif rew.get('error'):
            print(f"O60R_REW_ERROR={rew['error']}", flush=True)
    print(
        'O60R_INVENTORY_JSON='
        + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        flush=True,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
