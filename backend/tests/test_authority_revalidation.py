"""Round 14: post-update revalidation lane + degraded backup contract.

Simulates the real update hazard: a store written by the CURRENT build is
reopened under a "next build" that re-keyed the comparison algorithm
registration and the routing-profile seal. Everything the user could then
do — audit, backup, upgrade, revalidate — is exercised end-to-end against
real SQLite stores, no mocks on the data path.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_authorities import (
    CadRoutingProfile,
    build_routing_profile,
    build_wiring_check,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
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


def _seed_store(data_dir: Path) -> dict[str, str]:
    """Persist one scene + profile + wiring check + two datasets + one
    comparison under the CURRENT build (pre-re-key)."""

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
    return {
        'profile_id': profile.routing_profile_id,
        'check_id': check.check_id,
        'comparison_id': comparison.comparison_id,
    }


def _apply_rekey() -> None:
    """Simulate the next build's re-key, applied before any repository in
    this process touches the store (module constants behave like a real
    relaunch):

    - comparison algorithm registration moves to 'fr-compare-2' and the
      old version is dropped from the replay registry;
    - the routing-profile seal joins a new field, so every persisted
      profile fails its self-hash on parse.
    """

    from dataclasses import replace

    import htdt.comparison as comparison_mod
    from htdt.canonical_json import canonical_sha256

    new_identity = dict(comparison_mod.COMPARISON_ALGORITHM_IDENTITY)
    new_identity['algorithm_version'] = 'fr-compare-2'
    comparison_mod.ALGORITHM_VERSION = 'fr-compare-2'
    comparison_mod.COMPARISON_ALGORITHM_SHA256 = canonical_sha256(
        new_identity
    )

    def next_build_builder(*args, **kwargs):
        # In a real relaunch ComparisonResult.algorithm_version's dataclass
        # default binds the new constant; mirror that on the registered
        # builder so produced results carry the re-keyed version stamp.
        result = comparison_mod.compare_frequency_responses(*args, **kwargs)
        return replace(result, algorithm_version='fr-compare-2')

    comparison_mod._COMPARISON_RESULT_REPLAY.clear()
    comparison_mod._COMPARISON_RESULT_REPLAY['fr-compare-2'] = (
        comparison_mod.COMPARISON_ALGORITHM_SHA256,
        next_build_builder,
    )

    original_identity_payload = CadRoutingProfile.identity_payload

    def rekeyed_identity_payload(self):
        payload = original_identity_payload(self)
        payload['seal_version'] = 'routing-profile-2'
        return payload

    CadRoutingProfile.identity_payload = rekeyed_identity_payload


@pytest.fixture()
def rekeyed_store(tmp_path: Path):
    """A seeded store plus the simulated next-build re-key.

    The re-key mutates module-level constants (that IS the mechanism the
    flagged bug used), so the fixture restores them afterwards — under
    pytest-xdist a worker runs many test modules and a leaked 'fr-compare-2'
    build would pollute everything else in the process.
    """

    import htdt.comparison as comparison_mod

    saved = (
        comparison_mod.ALGORITHM_VERSION,
        comparison_mod.COMPARISON_ALGORITHM_SHA256,
        dict(comparison_mod._COMPARISON_RESULT_REPLAY),
        CadRoutingProfile.identity_payload,
    )
    data_dir = tmp_path / 'data'
    ids = _seed_store(data_dir)
    _apply_rekey()
    try:
        yield data_dir, data_dir / 'cad-scenes.sqlite3', ids
    finally:
        (
            comparison_mod.ALGORITHM_VERSION,
            comparison_mod.COMPARISON_ALGORITHM_SHA256,
        ) = saved[0], saved[1]
        comparison_mod._COMPARISON_RESULT_REPLAY.clear()
        comparison_mod._COMPARISON_RESULT_REPLAY.update(saved[2])
        CadRoutingProfile.identity_payload = saved[3]


def _audit(db: Path):
    from htdt.native_authority_audit import audit_native_authority_graph

    return audit_native_authority_graph(db)


def _failing_keys(report) -> set[tuple[str, str]]:
    return {(d.authority, d.record_ref) for d in report.diagnostics}


def test_rekey_strands_every_authority_kind(rekeyed_store):
    """Replay assertion: profile, wiring check, and comparison all go
    stale — nothing readable is silently kept."""

    _, db, ids = rekeyed_store
    report = _audit(db)
    assert not report.ok
    assert _failing_keys(report) == {
        ('routing_profile', ids['profile_id']),
        ('wiring_check', ids['check_id']),
        ('measurement_comparison', ids['comparison_id']),
    }


def test_revalidate_recovers_rekeyed_authorities(rekeyed_store):
    """The revalidation lane re-derives all three rows under the new
    build's identities; a fresh audit is clean and canonical reads work."""

    from htdt.authority_revalidation import revalidate_native_authority_graph

    data_dir, db, ids = rekeyed_store
    report = revalidate_native_authority_graph(db)

    assert report.resolved, report.summary_ja()
    assert report.audit.ok
    assert len(report.revalidated) == 3
    assert not report.kept_stale

    scene_repository = SceneRepository(db)
    measurements = CadMeasurementRepository(scene_repository)
    quality = CadMeasurementQualityRepository(measurements)
    # Canonical reads that were refusing now return parsed records.
    assert quality.get_routing_profile(ids['profile_id']) is not None
    assert quality.get_wiring_check(ids['check_id']) is not None
    assert measurements.get_comparison(ids['comparison_id']) is not None
    assert quality.list_routing_profiles('fixture-f1')
    assert quality.list_wiring_checks('fixture-f1')
    assert measurements.list_comparisons('fixture-f1')


def test_revalidate_keeps_semantically_tampered_rows_stale(rekeyed_store):
    """A row whose stored semantics changed cannot be honestly re-derived:
    it stays stale, its bytes are untouched, and the reason is explicit."""

    from htdt.authority_revalidation import revalidate_native_authority_graph

    _, db, ids = rekeyed_store
    with sqlite3.connect(db) as connection:
        payload_json = connection.execute(
            'SELECT payload_json FROM cad_routing_profiles '
            'WHERE routing_profile_id=?',
            (ids['profile_id'],),
        ).fetchone()[0]
        tampered = json.loads(payload_json)
        tampered['entries'][0]['verification'] = 'tampered-after-save'
        connection.execute(
            'UPDATE cad_routing_profiles SET payload_json=? '
            'WHERE routing_profile_id=?',
            (json.dumps(tampered), ids['profile_id']),
        )

    report = revalidate_native_authority_graph(db)
    profile_outcome = next(
        o for o in report.outcomes if o.record_ref == ids['profile_id']
    )
    assert profile_outcome.action == 'kept_stale'
    assert profile_outcome.detail
    # Wiring check still binds the unrecoverable profile; comparison
    # re-derives independently.
    by_ref = {o.record_ref: o.action for o in report.outcomes}
    assert by_ref[ids['comparison_id']] == 'revalidated'
    # The tampered bytes stay exactly as they were — no silent promote.
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            'SELECT payload_json FROM cad_routing_profiles '
            'WHERE routing_profile_id=?',
            (ids['profile_id'],),
        ).fetchone()[0] == json.dumps(tampered)
    assert not _audit(db).ok


def test_revalidate_comparison_keeps_stale_when_result_changed(rekeyed_store):
    """Persisted comparison output that no longer matches a replay over the
    stored inputs is kept stale, not promoted under the new algorithm."""

    from htdt.authority_revalidation import revalidate_native_authority_graph

    _, db, ids = rekeyed_store
    with sqlite3.connect(db) as connection:
        result_json = connection.execute(
            'SELECT result_json FROM cad_measurement_comparisons '
            'WHERE comparison_id=?',
            (ids['comparison_id'],),
        ).fetchone()[0]
        tampered = json.loads(result_json)
        tampered['difference_db'] = [
            value + 0.01 for value in tampered['difference_db']
        ]
        connection.execute(
            'UPDATE cad_measurement_comparisons SET result_json=? '
            'WHERE comparison_id=?',
            (json.dumps(tampered), ids['comparison_id']),
        )

    report = revalidate_native_authority_graph(db)
    outcome = next(
        o for o in report.outcomes if o.record_ref == ids['comparison_id']
    )
    assert outcome.action == 'kept_stale'
    assert not _audit(db).ok


def test_plain_backup_refuses_but_degraded_backup_roundtrips(
    rekeyed_store, tmp_path: Path
):
    """The flagged trap: --backup used to refuse, leaving no export path.
    allow_stale writes a degraded archive whose manifest declares every
    failing row; restore lands exactly that staleness and revalidation
    repairs it."""

    from htdt.native_authority_audit import AuthorityAuditError
    from htdt.native_backup import (
        create_backup,
        inspect_backup,
        restore_backup,
        validate_backup,
    )

    data_dir, db, _ = rekeyed_store
    with pytest.raises(AuthorityAuditError):
        create_backup(data_dir, tmp_path / 'plain.htdt-backup')

    backup_path = tmp_path / 'degraded.htdt-backup'
    manifest = create_backup(data_dir, backup_path, allow_stale=True)
    assert manifest.degraded
    assert len(manifest.stale_authorities) == 3
    assert manifest.identity_payload()['stale_authorities']
    # Validation and inspection accept a well-formed degraded archive.
    assert validate_backup(backup_path).degraded
    inspected, _ = inspect_backup(backup_path)
    assert len(inspected.stale_authorities) == 3

    restored_dir = tmp_path / 'restored'
    restore_backup(restored_dir, backup_path)
    restored_db = restored_dir / 'cad-scenes.sqlite3'
    restored_audit = _audit(restored_db)
    assert not restored_audit.ok
    # Exactly the declared stale set survives — no more, no less.
    declared = {
        (entry.authority, entry.record_ref)
        for entry in manifest.stale_authorities
    }
    assert _failing_keys(restored_audit) == declared

    # The restored copy is repairable: revalidation re-derives it.
    from htdt.authority_revalidation import revalidate_native_authority_graph

    recovery = revalidate_native_authority_graph(restored_db)
    assert recovery.resolved


def test_execute_native_upgrade_survives_stale_evidence(
    rekeyed_store, tmp_path: Path
):
    """Schema upgrade over stranded evidence used to die at the recovery
    snapshot (fail-closed create_backup) and lock the app out forever.
    Now the snapshot declares the stale set, the upgrade completes, and
    the marker records what was tolerated."""

    from htdt.native_upgrade import (
        execute_native_upgrade,
        list_upgrade_events,
    )

    data_dir, db, _ = rekeyed_store
    with sqlite3.connect(db) as connection:
        connection.execute(
            'UPDATE native_schema_metadata SET schema_version=9 '
            'WHERE singleton=1'
        )
        connection.execute(
            'DELETE FROM native_schema_migrations WHERE schema_version >= 10'
        )

    event = execute_native_upgrade(data_dir)
    assert event.from_schema == 9
    assert event.to_schema == 10
    assert event.stale_authority_count == 3
    # The journal keeps the same declaration (the live marker is cleared
    # on verified completion by design).
    journaled = list_upgrade_events(data_dir)
    assert journaled[0].stale_authority_count == 3

    # Upgrade tolerated the staleness — it did not repair or hide it.
    assert not _audit(db).ok
    from htdt.authority_revalidation import revalidate_native_authority_graph

    assert revalidate_native_authority_graph(db).resolved


def test_launch_marker_tracks_build_identity(tmp_path: Path):
    """First-run-after-update detection: a changed or missing marker means
    'run the post-update audit once'."""

    from htdt.authority_revalidation import (
        launch_build_changed,
        read_launch_marker,
        write_launch_marker,
    )

    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True)
    assert launch_build_changed(data_dir)

    assert write_launch_marker(data_dir) is not None
    assert not launch_build_changed(data_dir)

    marker_path = data_dir / 'htdt-launch-build.json'
    marker = read_launch_marker(data_dir)
    marker['display_version'] = '0.0.0-older'
    marker_path.write_text(json.dumps(marker), encoding='utf-8')
    assert launch_build_changed(data_dir)

    marker_path.unlink()
    assert launch_build_changed(data_dir)


def test_summary_ja_points_at_the_real_backup_checkbox_label(tmp_path: Path):
    """Round-14 regression guard: the kept-stale guidance must name the
    checkbox as it actually reads in the data-management page, or the user
    is pointed at a control that does not exist."""
    from htdt.authority_revalidation import (
        RevalidationOutcome,
        RevalidationReport,
    )
    from htdt.native_authority_audit import (
        AuthorityAuditDiagnostic,
        AuthorityAuditReport,
    )

    diagnostic = AuthorityAuditDiagnostic(
        authority='routing_profile',
        record_ref='rp-stale-1',
        failure_class='noncanonical_derivation',
        dependency='signal_path',
        message='stored signature differs',
    )
    report = RevalidationReport(
        database_path=tmp_path / 'db.sqlite3',
        outcomes=(
            RevalidationOutcome(
                authority='routing_profile',
                record_ref='rp-stale-1',
                action='kept_stale',
                detail='再導出できませんでした',
            ),
        ),
        audit=AuthorityAuditReport(
            database_path=tmp_path / 'db.sqlite3',
            checked=(('cad_routing_profiles', 1),),
            diagnostics=(diagnostic,),
        ),
    )

    summary = report.summary_ja()
    # The real checkbox label on the operations card is
    # 「検証を通過しない記録を含めてバックアップする（対象はマニフェストに明記されます）」
    assert '検証を通過しない記録を含めてバックアップ' in summary
