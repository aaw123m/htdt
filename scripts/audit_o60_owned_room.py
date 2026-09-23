from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_model_validation_service import CadModelValidationService
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_campaign_repository import CadValidationCampaignRepository
from htdt.cad_validation_campaign_service import CadValidationCampaignService


DATABASE_NAME = 'cad-scenes.sqlite3'


def _readonly_snapshot(source: Path, destination: Path) -> None:
    uri = source.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        source_connection.execute('PRAGMA query_only=ON')
        integrity = source_connection.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise AssertionError(f'source database integrity_check failed: {integrity!r}')
        foreign_keys = source_connection.execute('PRAGMA foreign_key_check').fetchall()
        if foreign_keys:
            raise AssertionError(f'source database foreign_key_check failed: {foreign_keys!r}')
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)


def _build_repositories(database: Path):
    scene = SceneRepository(database)
    search = CadSearchRepository(scene)
    measurement = CadMeasurementRepository(scene)
    roomsim = CadRoomSimRepository(scene, search)
    objective = CadObjectiveRepository(scene, search)
    campaign = CadValidationCampaignRepository(search, measurement)
    validation_service = CadModelValidationService(
        search,
        roomsim,
        measurement,
        objective,
    )
    campaign_service = CadValidationCampaignService(
        campaign,
        roomsim,
        measurement,
        objective,
        validation_service,
    )
    validation = CadModelValidationRepository(
        search,
        roomsim,
        measurement,
        objective,
    )
    return (
        scene,
        search,
        measurement,
        roomsim,
        objective,
        campaign,
        campaign_service,
        validation,
    )


def _check_gate(record) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if record.evidence_scope != 'owned_room':
        reasons.append('validation evidence_scope is not owned_room')
    if record.recommendation_gate != 'eligible':
        reasons.append(f'recommendation gate is {record.recommendation_gate}')
    if record.gate_reasons:
        reasons.extend(f'record gate reason: {reason}' for reason in record.gate_reasons)
    if record.residual_gate != 'pass':
        reasons.append(f'residual gate is {record.residual_gate}')
    for check in record.trend_checks:
        if check.gate != 'pass':
            reasons.append(f'trend {check.objective_id} is {check.gate}')
    for check in record.sensitivity_checks:
        if check.gate != 'pass':
            reasons.append(
                f'sensitivity {check.objective_id} '
                f'{check.candidate_a_id}/{check.candidate_b_id} is {check.gate}'
            )
    for check in record.repeatability_checks:
        if check.gate != 'pass':
            reasons.append(
                f'repeatability {check.scene_revision_id} is {check.gate}'
            )
    for check in record.separation_checks:
        if check.gate != 'pass':
            reasons.append(
                f'separation {check.candidate_a_id}/{check.candidate_b_id} is {check.gate}'
            )
    for check in record.applicability_checks:
        if not check.passed:
            reasons.append(f'applicability {check.code} failed: {check.detail or ""}')
    return not reasons, tuple(reasons)


def _candidate_report(readiness) -> list[dict[str, object]]:
    return [
        {
            'candidate_id': item.candidate_id,
            'split': item.split,
            'prediction_attempt_id': item.prediction_attempt_id,
            'measurement_plan_id': item.measurement_plan_id,
            'measurement_ids': list(item.measurement_ids),
            'primary_measurement_id': item.primary_measurement_id,
            'predicted_evaluation_id': item.predicted_evaluation_id,
            'measured_evaluation_id': item.measured_evaluation_id,
            'missing_reasons': list(item.missing_reasons),
        }
        for item in readiness.candidates
    ]


def audit(
    data_dir: Path,
    campaign_id: str,
    validation_id: str | None,
) -> tuple[bool, dict[str, object]]:
    source_database = data_dir / DATABASE_NAME
    if not source_database.is_file():
        raise FileNotFoundError(f'native database is missing: {source_database}')

    with tempfile.TemporaryDirectory(prefix='htdt-o60r-audit-') as temp_name:
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

        campaign = campaign_repository.get(campaign_id)
        if campaign is None:
            raise AssertionError(f'campaign does not exist: {campaign_id}')
        registration = campaign_repository.get_registration(campaign_id)
        if registration is None:
            raise AssertionError(
                f'campaign has no durable registration: {campaign_id}'
            )

        readiness = campaign_service.readiness(campaign_id)
        # The listing is payload-only campaign filtering, so it uses the
        # non-authoritative history view: a stale record from another
        # campaign must not abort this audit. The selected record is always
        # re-attested below — either by latest_eligible_for_search_spec or
        # by the save/get replay on the snapshot.
        validations = tuple(
            record
            for record in validation_repository.inspect_for_search_spec(
                campaign.search_spec_id
            )
            if record.campaign_id == campaign_id
        )
        latest = validation_repository.latest_eligible_for_search_spec(
            campaign.search_spec_id
        )
        if latest is None:
            raise AssertionError('SearchSpec has no eligible owned-room ValidationRecord')
        if latest.campaign_id != campaign_id:
            raise AssertionError(
                'current O70 entry belongs to another campaign: '
                f'{latest.campaign_id} != {campaign_id}'
            )

        if validation_id is None:
            record = latest
        else:
            record = next(
                (item for item in validations if item.validation_id == validation_id),
                None,
            )
            if record is None:
                raise AssertionError(
                    f'ValidationRecord does not exist in campaign: {validation_id}'
                )
            if latest.validation_id != record.validation_id:
                raise AssertionError(
                    'selected ValidationRecord is not the current O70 entry record: '
                    f'{record.validation_id} != {latest.validation_id}'
                )

        if record.campaign_sha256 != campaign.campaign_sha256:
            raise AssertionError('ValidationRecord campaign SHA does not match campaign')

        # Full save-time validation is intentionally re-run on the temporary snapshot.
        # Deleting/reinserting here cannot mutate the source DB, which was opened mode=ro.
        with closing(sqlite3.connect(snapshot_database)) as connection, connection:
            connection.execute(
                'DELETE FROM cad_model_validations WHERE validation_id=?',
                (record.validation_id,),
            )
        validation_repository.save(record)
        reopened = validation_repository.get(record.validation_id)
        if reopened != record:
            raise AssertionError('ValidationRecord changed during full authority replay')

        gate_ok, gate_reasons = _check_gate(record)
        passed = readiness.evidence_ready and gate_ok

        report: dict[str, object] = {
            'campaign_id': campaign.campaign_id,
            'campaign_sha256': campaign.campaign_sha256,
            'document_id': campaign.document_id,
            'search_spec_id': campaign.search_spec_id,
            'search_spec_sha256': campaign.search_spec_sha256,
            'candidate_set_sha256': campaign.candidate_set_sha256,
            'model_id': campaign.model_id,
            'model_version': campaign.model_version,
            'campaign_created_at_utc': campaign.created_at_utc,
            'campaign_registered_at_utc': registration.registered_at_utc,
            'campaign_registration_id': registration.registration_id,
            'campaign_registration_sha256': registration.registration_sha256,
            'candidate_count': len(campaign.candidates),
            'candidates': _candidate_report(readiness),
            'readiness_missing_reasons': list(readiness.missing_reasons),
            'evidence_ready': readiness.evidence_ready,
            'validation_id': record.validation_id,
            'validation_sha256': record.validation_sha256,
            'validation_created_at_utc': record.created_at_utc,
            'evidence_scope': record.evidence_scope,
            'holdout_rms_db': record.holdout_rms_db,
            'max_holdout_rms_db': record.max_holdout_rms_db,
            'residual_gate': record.residual_gate,
            'trend_checks': [check.model_dump(mode='json') for check in record.trend_checks],
            'sensitivity_checks': [
                check.model_dump(mode='json') for check in record.sensitivity_checks
            ],
            'repeatability_checks': [
                check.model_dump(mode='json') for check in record.repeatability_checks
            ],
            'separation_checks': [
                check.model_dump(mode='json') for check in record.separation_checks
            ],
            'applicability_checks': [
                check.model_dump(mode='json') for check in record.applicability_checks
            ],
            'applicability_authorities': [
                {
                    'code': check.code,
                    'passed': check.passed,
                    'evaluator_id': check.evaluator_id,
                    'evaluator_version': check.evaluator_version,
                    'subject': check.subject(),
                    'evidence_refs': [
                        ref.model_dump(mode='json') for ref in check.evidence_refs
                    ],
                    'decision_sha256': check.decision_sha256,
                }
                for check in record.applicability_checks
            ],
            'recommendation_gate': record.recommendation_gate,
            'gate_reasons': list(record.gate_reasons),
            'audit_gate_reasons': list(gate_reasons),
            # Native integrity scan over every persisted O60 record in the
            # SearchSpec through the shared _validate_record authority path.
            'integrity_problems': validation_repository.integrity_problems(
                campaign.search_spec_id
            ),
            'source_database_readonly': True,
            'full_save_authority_replayed_on_snapshot': True,
            'passed': passed,
        }
        return passed, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Read-only O60 owned-room campaign/ValidationRecord audit.'
    )
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--campaign-id', required=True)
    parser.add_argument('--validation-id', default=None)
    parser.add_argument('--report-json', type=Path, default=None)
    args = parser.parse_args()

    try:
        passed, report = audit(
            args.data_dir.resolve(),
            str(args.campaign_id),
            None if args.validation_id is None else str(args.validation_id),
        )
    except Exception as exc:
        print(f'O60R_AUDIT_ERROR={type(exc).__name__}: {exc}', flush=True)
        print('O60R_OWNED_ROOM_AUDIT_RESULT=FAIL', flush=True)
        return 1

    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )

    for key in (
        'campaign_id',
        'campaign_sha256',
        'search_spec_id',
        'search_spec_sha256',
        'candidate_set_sha256',
        'model_id',
        'model_version',
        'validation_id',
        'validation_sha256',
        'evidence_scope',
        'residual_gate',
        'recommendation_gate',
    ):
        print(f'O60R_{key.upper()}={report[key]}', flush=True)
    print(f"O60R_EVIDENCE_READY={report['evidence_ready']}", flush=True)
    for authority in report['applicability_authorities']:
        code_key = ''.join(
            char if char.isalnum() else '_'
            for char in str(authority['code'])
        ).upper()
        refs = ';'.join(
            f"{ref['source_kind']}:{ref['source_id']}:{ref['source_sha256']}"
            for ref in authority['evidence_refs']
        )
        print(
            f'O60R_APPLICABILITY_{code_key}='
            f"{authority['passed']}|"
            f"{authority['evaluator_id']}@{authority['evaluator_version']}|"
            f'{refs}',
            flush=True,
        )
    print(
        'O60R_SOURCE_DATABASE_READONLY='
        f"{report['source_database_readonly']}",
        flush=True,
    )
    print(
        'O60R_FULL_SAVE_AUTHORITY_REPLAYED='
        f"{report['full_save_authority_replayed_on_snapshot']}",
        flush=True,
    )
    print(
        'O60R_AUDIT_JSON='
        + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        flush=True,
    )
    print(
        'O60R_OWNED_ROOM_AUDIT_RESULT=' + ('PASS' if passed else 'FAIL'),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
