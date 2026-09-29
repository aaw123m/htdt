"""REV14-UPDATE: end-to-end replay of the app-update user journey.

Two phases run as separate interpreter invocations so module-level
constants behave like a real relaunch:

  phase=seed   — build a real store under the CURRENT build: scene,
                 routing profile, wiring check, datasets, comparison.
  phase=rekey  — apply the "next build" re-key patches BEFORE any
                 repository touches the store, then enumerate every
                 user-visible consequence.

Re-key patches model the flagged mechanism (#848-style): the next build
bumps the comparison algorithm registration and the routing-profile
seal, so persisted evidence pinned to the old identities goes stale.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

DATA_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r'C:\t\rev14-store')


def _header(title: str) -> None:
    print(f'\n=== {title} ===')


def _probe(label: str, fn) -> None:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - enumerate, don't abort
        print(f'{label}: REFUSED [{type(exc).__name__}] {str(exc)[:220]}')
        return
    print(f'{label}: OK -> {str(result)[:220]}')


def seed(data_dir: Path) -> None:
    from hashlib import sha256

    from htdt.cad_measurement_models import CadFrequencyResponseDataset
    from htdt.cad_measurement_quality_repository import (
        CadMeasurementQualityRepository,
    )
    from htdt.cad_measurement_authorities import (
        build_routing_profile,
        build_wiring_check,
    )
    from htdt.cad_measurement_repository import CadMeasurementRepository
    from htdt.cad_measurements import (
        HTDT_DECLARED_IMPORTER_VERSION,
        declared_fr_raw,
        measurement_record_for_revision,
    )
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.comparison import (
        FrequencyResponse,
        compare_frequency_responses,
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / 'cad-scenes.sqlite3'
    scene_repository = SceneRepository(db)
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )

    profile = build_routing_profile(
        profile_name='main-theater',
        document_id='fixture-f1',
        scene_revision_id=revision.revision_id,
        entries=(
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'front_left',
                'expected_speaker_ids': ('speaker-fl',),
                'observed_speaker_ids': ('speaker-fl',),
                'verification': 'verified',
                'verified_at_utc': '2026-09-20T00:00:00+00:00',
                'avr_configuration_id': 'avr-xt32-movie',
            },
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FR',
                'hardware_channel_index': 1,
                'logical_role': 'front_right',
                'expected_speaker_ids': ('speaker-fr',),
                'observed_speaker_ids': ('speaker-fr',),
                'verification': 'verified',
                'verified_at_utc': '2026-09-20T00:00:00+00:00',
                'avr_configuration_id': 'avr-xt32-movie',
            },
        ),
    )
    quality_repository.save_routing_profile(profile)

    check = build_wiring_check(
        document_id='fixture-f1',
        check_kind='routing',
        method='REW HDMI channel sweep',
        result='PASS',
        measured_at_utc='2026-09-20T00:00:00+00:00',
        scene_revision_id=revision.revision_id,
        scene_revision_sha256=revision.content_hash,
        routing_profile=profile,
        routing_profile_ref=None,
        expected_output_reference='C:FL',
        expected_speaker_ids=('speaker-fl',),
        observed_output_reference='C:FL',
        observed_speaker_ids=('speaker-fl',),
        evidence_refs=('manual:channel-sweep',),
        operator='installer',
    )
    quality_repository.save_wiring_check(check)

    def save_dataset(measurement_id: str, levels):
        frequencies = (20.0, 40.0, 80.0, 160.0)
        raw = declared_fr_raw(
            frequency_hz=frequencies, level_db=levels, phase_status='absent'
        )
        record = measurement_record_for_revision(
            revision,
            'point-mlp',
            measurement_id=measurement_id,
            evidence_type='measured',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
            routing_evidence='verified',
            imported_at='2026-09-19T00:00:00+00:00',
            source_kind='unknown',
            external_source_id=f'rew-{measurement_id}',
        )
        dataset = CadFrequencyResponseDataset(
            dataset_id=f'dataset-{measurement_id}',
            measurement_id=measurement_id,
            frequency_hz=frequencies,
            level_db=levels,
            phase_status='absent',
            source_sha256=sha256(raw).hexdigest(),
            importer_version=HTDT_DECLARED_IMPORTER_VERSION,
        )
        measurement_repository.save(
            record,
            dataset,
            raw_filename=f'{measurement_id}.txt',
            raw_bytes=raw,
        )
        return dataset

    dataset_a = save_dataset('meas-a', (70.0, 71.0, 69.0, 72.0))
    dataset_b = save_dataset('meas-b', (69.0, 70.0, 68.0, 71.0))
    result = compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0,
        160.0,
    )
    comparison = measurement_repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, result
    )
    print(f'seeded: profile={profile.routing_profile_id[:12]} '
          f'check={check.check_id[:12]} cmp={comparison.comparison_id[:12]}')
    print(f'store: {db}')


def rekey(data_dir: Path) -> None:
    # ---- next-build re-key, applied before any repository opens the store.
    # Mechanism 1: the algorithm registration moves to a new identity SHA
    # (same version slot re-keyed — the '#848 style' hazard) AND the old
    # version is dropped so historical results are no longer replayable.
    import htdt.comparison as comparison_mod
    from htdt.canonical_json import canonical_sha256

    from dataclasses import replace

    old_version = comparison_mod.ALGORITHM_VERSION
    new_identity = dict(comparison_mod.COMPARISON_ALGORITHM_IDENTITY)
    new_identity['algorithm_version'] = 'fr-compare-2'
    comparison_mod.ALGORITHM_VERSION = 'fr-compare-2'
    comparison_mod.COMPARISON_ALGORITHM_SHA256 = canonical_sha256(
        new_identity
    )

    def next_build_builder(*args, **kwargs):
        # A real next build binds the new constant in the dataclass
        # default; mirror it so produced results carry the new stamp.
        result = comparison_mod.compare_frequency_responses(*args, **kwargs)
        return replace(result, algorithm_version='fr-compare-2')

    comparison_mod._COMPARISON_RESULT_REPLAY.clear()
    comparison_mod._COMPARISON_RESULT_REPLAY['fr-compare-2'] = (
        comparison_mod.COMPARISON_ALGORITHM_SHA256,
        next_build_builder,
    )
    print(f're-key: comparison {old_version} -> fr-compare-2, '
          'old registration dropped')

    # Mechanism 2: the routing-profile seal joins a new field — every
    # persisted profile now fails its self-hash on parse.
    from htdt.cad_measurement_authorities import CadRoutingProfile

    original_identity_payload = CadRoutingProfile.identity_payload

    def rekeyed_identity_payload(self):
        payload = original_identity_payload(self)
        payload['seal_version'] = 'routing-profile-2'
        return payload

    CadRoutingProfile.identity_payload = rekeyed_identity_payload
    print('re-key: routing profile seal gains seal_version field')

    db = data_dir / 'cad-scenes.sqlite3'

    _header('audit (what the store now looks like to the new build)')
    from htdt.native_authority_audit import audit_native_authority_graph

    report = audit_native_authority_graph(db)
    print(report.summary())
    for diagnostic in report.diagnostics:
        print(
            f'  [{diagnostic.authority}:{diagnostic.record_ref[:48]}] '
            f'{diagnostic.failure_class} — {diagnostic.message[:160]}'
        )

    _header('--backup (user tries to export before re-validating)')
    from htdt.native_backup import create_backup

    _probe(
        'create_backup (fail-closed, unchanged default)',
        lambda: create_backup(db.parent, data_dir / 'out.htdt-backup'),
    )
    _probe(
        'create_backup allow_stale=True (degraded archive)',
        lambda: create_backup(
            db.parent,
            data_dir / 'out-degraded.htdt-backup',
            allow_stale=True,
        ),
    )
    degraded = data_dir / 'out-degraded.htdt-backup'
    if degraded.is_file():
        from htdt.native_backup import inspect_backup

        manifest, _ = inspect_backup(degraded)
        print(
            f'  degraded manifest: {len(manifest.stale_authorities)} '
            'declared stale rows'
        )
        for entry in manifest.stale_authorities:
            print(f'    {entry.authority}:{entry.record_ref[:40]} '
                  f'{entry.failure_class}')

    _header('upgrade lane (schema migration at v10)')
    from htdt.native_upgrade import (
        execute_native_upgrade,
        plan_native_upgrade,
    )

    _probe('plan_native_upgrade', lambda: plan_native_upgrade(data_dir))
    _probe(
        'execute_native_upgrade',
        lambda: execute_native_upgrade(data_dir),
    )

    _header('repository reads (what the workspace hits)')
    from htdt.cad_repository import SceneRepository
    from htdt.cad_measurement_repository import CadMeasurementRepository
    from htdt.cad_measurement_quality_repository import (
        CadMeasurementQualityRepository,
    )

    scene_repository = SceneRepository(db)
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )

    _probe(
        'get_comparison',
        lambda: measurement_repository.get_comparison(
            _first_id(
                db,
                'SELECT comparison_id FROM cad_measurement_comparisons',
            )
        ),
    )
    _probe(
        'list_comparisons',
        lambda: measurement_repository.list_comparisons('fixture-f1'),
    )
    _probe(
        'get_routing_profile',
        lambda: quality_repository.get_routing_profile(
            _first_id(
                db,
                'SELECT routing_profile_id FROM cad_routing_profiles',
            )
        ),
    )
    _probe(
        'list_routing_profiles',
        lambda: quality_repository.list_routing_profiles('fixture-f1'),
    )
    _probe(
        'get_wiring_check',
        lambda: quality_repository.get_wiring_check(
            _first_id(db, 'SELECT check_id FROM cad_wiring_checks')
        ),
    )
    _probe(
        'list_wiring_checks',
        lambda: quality_repository.list_wiring_checks('fixture-f1'),
    )

    _header('再検証 lane (the new migration path)')
    from htdt.authority_revalidation import revalidate_native_authority_graph

    report = revalidate_native_authority_graph(db)
    print(report.summary_ja())
    for outcome in report.outcomes:
        print(f'  [{outcome.authority}:{outcome.record_ref[:40]}] '
              f'{outcome.action} — {outcome.detail[:140]}')
    _probe(
        'create_backup (post-revalidation)',
        lambda: create_backup(db.parent, data_dir / 'out-clean.htdt-backup'),
    )


def _first_id(db: Path, sql: str) -> str:
    import sqlite3

    with sqlite3.connect(db) as connection:
        return str(connection.execute(sql).fetchone()[0])


def main() -> None:
    phase = sys.argv[1]
    data_dir = DATA_DIR
    if phase == 'seed':
        seed(data_dir)
    elif phase == 'rekey':
        rekey(data_dir)
    else:
        raise SystemExit(f'unknown phase {phase}')


if __name__ == '__main__':
    main()
