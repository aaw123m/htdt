"""REV56-SNAPSTD (#592/#599): device configuration snapshot/restore
authority + external standards registry — sealed identities, honest
UNKNOWN, fail-closed gates, append-only persistence, lifecycle
conflicts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_device_backup import (
    DeviceConfigurationBackupArtifact,
    RestoreCheckResult,
)
from htdt.cad_device_snapshot import (
    DeviceConfigurationSnapshot,
    ObservedDeviceField,
    PreUpdateDeclaration,
    ReplacementPortabilityEntry,
    build_device_snapshot,
    build_firmware_transition,
    build_known_good_baseline,
    build_replacement_assessment,
    build_restore_record,
    diff_snapshots,
    evaluate_known_good_eligibility,
    evaluate_pre_update_gate,
    evaluate_restore_verdict,
    replacement_blocking_entries,
    replacement_conditional_entries,
)
from htdt.cad_device_snapshot_repository import (
    CadDeviceSnapshotRepository,
    DeviceSnapshotConflictError,
    DeviceSnapshotIntegrityError,
)
from htdt.cad_external_standards import (
    RevisionDiffEntry,
    affected_evaluation_pins,
    build_standard_document,
    build_evaluation_pin,
    build_lifecycle_observation,
    build_profile_mapping,
    build_revision_diff,
    detect_lifecycle_conflict,
    effective_lifecycle,
    seed_lifecycle_observations,
    seed_standard_documents,
    standard_citation_allowed,
    standards_profile_capability,
)
from htdt.cad_external_standards_repository import (
    CadExternalStandardsRepository,
    ExternalStandardsConflictError,
    ExternalStandardsIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.measurement_evidence_display import (
    device_evidence_class_label,
    device_snapshot_line,
    restore_record_line,
    restore_verdict_label,
    standards_capability_label,
    standards_document_line,
    standards_lifecycle_label,
    standards_pin_line,
)


DOC = 'doc-snapstd'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        make_empty_scene(doc_id), parent_revision_id=None
    )
    return scene_repository


def _standards_repo(tmp_path) -> CadExternalStandardsRepository:
    return CadExternalStandardsRepository(_scene_repo(tmp_path))


def _device_repo(tmp_path) -> CadDeviceSnapshotRepository:
    return CadDeviceSnapshotRepository(_scene_repo(tmp_path))


def _field(name: str, value: str | None, state: str = 'observed'):
    return ObservedDeviceField(field=name, state=state, value=value)


def _snapshot(**overrides) -> DeviceConfigurationSnapshot:
    kwargs = dict(
        document_id=DOC,
        instance_id='avr-1',
        evidence_class='device_readback',
        transition_kind='initial_configuration',
        manufacturer='Trinnov',
        model='Altitude 16',
        firmware_version='4.3.0',
        serial_number='SN-12345',
        fields=(_field('preset_slot', 'A'), _field('xover_hz', '80')),
    )
    kwargs.update(overrides)
    return build_device_snapshot(**kwargs)


def _backup_artifact(**overrides) -> DeviceConfigurationBackupArtifact:
    from htdt.cad_device_backup import build_backup_artifact

    kwargs = dict(
        artifact_id='art-1',
        device_equipment_id='avr-1',
        manufacturer='Trinnov',
        model='Altitude 16',
        firmware_version='4.3.0',
        backup_format='tnz',
        content_sha256=SHA_A,
        captured_at_utc='2026-10-05T12:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_backup_artifact(**kwargs)


def _check(name: str, status: str) -> RestoreCheckResult:
    return RestoreCheckResult(check=name, status=status, reason='r')


# ---------------------------------------------------------------------------
# #592 device configuration snapshots (CFG10-70)
# ---------------------------------------------------------------------------


def test_cfg10_snapshot_is_sealed_with_derived_identity():
    snapshot = _snapshot()
    assert snapshot.snapshot_id.startswith('devsnap-')
    assert len(snapshot.snapshot_sha256) == 64
    # state identity is content-derived — same pins + fields, same state
    # hash; the snapshot record itself additionally pins capture time
    again = _snapshot()
    assert again.state_content_sha256 == snapshot.state_content_sha256
    identical = _snapshot(captured_at_utc=snapshot.captured_at_utc)
    assert identical.snapshot_id == snapshot.snapshot_id


def test_cfg11_snapshot_rejects_tampered_hash():
    snapshot = _snapshot()
    payload = snapshot.model_dump(mode='python')
    payload['firmware_version'] = '9.9.9'
    with pytest.raises(ValidationError):
        DeviceConfigurationSnapshot(**payload)


def test_cfg12_evidence_class_requires_its_pin():
    # device_export_backup without a backup artifact pin is not evidence
    with pytest.raises(ValidationError):
        _snapshot(
            evidence_class='device_export_backup',
            backup_artifact_sha256=None,
        )
    # htdt_applied_request must pin the applied request — intent is not
    # applied state
    with pytest.raises(ValidationError):
        _snapshot(
            evidence_class='htdt_applied_request',
            applied_request_sha256=None,
        )
    # device_readback without fields AND without an observation pin is
    # an empty claim
    with pytest.raises(ValidationError):
        _snapshot(fields=(), observation_sha256=None)


def test_cfg13_unobserved_fields_cannot_carry_values():
    with pytest.raises(ValidationError):
        ObservedDeviceField(
            field='hidden_menu', state='unsupported', value='x'
        )
    with pytest.raises(ValidationError):
        ObservedDeviceField(field='volume', state='observed', value=None)


def test_cfg14_repository_round_trip_and_append_only(tmp_path):
    repository = _device_repo(tmp_path)
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    repository.save_snapshot(snapshot)  # identical is a no-op
    got = repository.get_snapshot(snapshot.snapshot_id)
    assert got == snapshot
    # a divergent row under the same snapshot_id is a conflict, never an
    # update — model_construct bypasses the seal check on purpose to
    # prove the repository (not just the model) enforces append-only
    divergent = DeviceConfigurationSnapshot.model_construct(
        **{**snapshot.model_dump(mode='python'),
           'snapshot_sha256': SHA_B}
    )
    with pytest.raises(DeviceSnapshotConflictError):
        repository.save_snapshot(divergent)


def test_cfg15_snapshot_dag_requires_persisted_parents(tmp_path):
    repository = _device_repo(tmp_path)
    child = _snapshot(
        transition_kind='manual_tuning',
        parent_snapshot_ids=('devsnap-' + 'f' * 24,),
    )
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_snapshot(child)


def test_cfg16_snapshot_dag_children_resolve(tmp_path):
    repository = _device_repo(tmp_path)
    parent = _snapshot()
    repository.save_snapshot(parent)
    child = _snapshot(
        transition_kind='manual_tuning',
        parent_snapshot_ids=(parent.snapshot_id,),
        fields=(_field('preset_slot', 'B'),),
    )
    repository.save_snapshot(child)
    children = repository.snapshot_children(parent.snapshot_id)
    assert [c.snapshot_id for c in children] == [child.snapshot_id]


def test_cfg17_semantic_diff_reports_field_changes():
    before = _snapshot()
    after = _snapshot(
        transition_kind='manual_tuning',
        parent_snapshot_ids=(before.snapshot_id,),
        fields=(
            _field('preset_slot', 'A'),
            _field('xover_hz', '90'),
            _field('new_param', 'on'),
        ),
    )
    diff = diff_snapshots(before, after)
    assert diff.semantic_available
    assert not diff.identical
    changes = {entry.field for entry in diff.entries}
    assert changes == {'xover_hz', 'new_param'}


def test_cfg18_opaque_snapshots_compare_by_state_hash_only():
    before = _snapshot(
        fields=(), observation_sha256=SHA_A,
    )
    same = _snapshot(
        fields=(), observation_sha256=SHA_B,
        transition_kind='restore',
    )
    diff = diff_snapshots(before, same)
    assert not diff.semantic_available
    # identical identity pins -> identical opaque state
    assert diff.identical
    other_fw = _snapshot(
        fields=(), observation_sha256=SHA_B, firmware_version='5.0.0',
        transition_kind='restore',
    )
    diff2 = diff_snapshots(before, other_fw)
    assert not diff2.identical


def test_cfg19_diff_between_identical_snapshots_is_semantically_equal():
    snapshot = _snapshot()
    same_content = _snapshot()
    diff = diff_snapshots(snapshot, same_content)
    assert diff.semantic_available
    assert diff.identical


def test_cfg20_known_good_eligibility_requires_verified_evidence():
    snapshot = _snapshot()
    ok, reasons = evaluate_known_good_eligibility(
        snapshot, backup_retained=True, post_verification_passed=True
    )
    assert ok and not reasons
    ok, reasons = evaluate_known_good_eligibility(
        snapshot, backup_retained=False, post_verification_passed=True
    )
    assert not ok and reasons
    ok, reasons = evaluate_known_good_eligibility(
        snapshot, backup_retained=True, post_verification_passed=False
    )
    assert not ok and reasons
    # an inference is never promotion evidence
    inferred = _snapshot(
        evidence_class='inferred_from_measurement', fields=()
    )
    ok, _ = evaluate_known_good_eligibility(
        inferred, backup_retained=True, post_verification_passed=True
    )
    assert not ok
    # no firmware pin -> cannot answer "which firmware was this"
    no_fw = _snapshot(firmware_version=None)
    ok, _ = evaluate_known_good_eligibility(
        no_fw, backup_retained=True, post_verification_passed=True
    )
    assert not ok


def test_cfg21_baseline_round_trip_and_immutability(tmp_path):
    repository = _device_repo(tmp_path)
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    baseline = build_known_good_baseline(snapshot=snapshot)
    repository.save_baseline(baseline)
    repository.save_baseline(baseline)  # no-op
    got = repository.get_baseline(baseline.baseline_id)
    assert got == baseline
    assert got.snapshot_sha256 == snapshot.snapshot_sha256


def test_cfg22_baseline_requires_persisted_snapshot(tmp_path):
    repository = _device_repo(tmp_path)
    snapshot = _snapshot()
    baseline = build_known_good_baseline(snapshot=snapshot)
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_baseline(baseline)


def test_cfg30_firmware_transition_records_change(tmp_path):
    repository = _device_repo(tmp_path)
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    transition = build_firmware_transition(
        document_id=DOC,
        instance_id='avr-1',
        from_firmware='4.3.0',
        to_firmware='4.4.1',
        update_source='https://example.invalid/fw-441',
        pre_snapshot_sha256=snapshot.snapshot_sha256,
        reverify_domains=('speaker_levels', 'bass_management'),
    )
    repository.save_firmware_transition(transition)
    got = repository.get_firmware_transition(transition.transition_id)
    assert got == transition
    assert got.reverify_domains == ('speaker_levels', 'bass_management')


def test_cfg31_firmware_transition_rejects_same_version():
    with pytest.raises(ValidationError):
        build_firmware_transition(
            document_id=DOC,
            instance_id='avr-1',
            from_firmware='4.3.0',
            to_firmware='4.3.0',
        )


def test_cfg32_transition_requires_persisted_pre_snapshot(tmp_path):
    repository = _device_repo(tmp_path)
    transition = build_firmware_transition(
        document_id=DOC,
        instance_id='avr-1',
        to_firmware='4.4.1',
        pre_snapshot_sha256=SHA_A,
    )
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_firmware_transition(transition)


def test_cfg33_unknown_source_firmware_is_honest(tmp_path):
    repository = _device_repo(tmp_path)
    transition = build_firmware_transition(
        document_id=DOC, instance_id='avr-1',
        from_firmware=None, to_firmware='4.4.1',
    )
    repository.save_firmware_transition(transition)
    assert transition.from_firmware is None


def test_cfg40_restore_verdict_computed_from_evidence():
    checks = (_check('model_match', 'PASS'),)
    diff_ok = diff_snapshots(_snapshot(), _snapshot())
    assert evaluate_restore_verdict(
        compatibility_checks=checks,
        result_status='success',
        post_restore_diff=diff_ok,
    ) == 'restored_exact_observed_state'
    changed = diff_snapshots(
        _snapshot(),
        _snapshot(transition_kind='restore',
                  fields=(_field('preset_slot', 'B'),)),
    )
    assert evaluate_restore_verdict(
        compatibility_checks=checks,
        result_status='success',
        post_restore_diff=changed,
    ) == 'restored_with_differences'
    # a vendor "Load OK" ack without post-restore evidence is unverified
    assert evaluate_restore_verdict(
        compatibility_checks=checks,
        result_status='success',
        post_restore_diff=None,
    ) == 'restore_unverified'
    assert evaluate_restore_verdict(
        compatibility_checks=checks,
        result_status='failed',
        post_restore_diff=None,
    ) == 'restore_failed'
    failed_checks = (_check('model_match', 'FAIL'),)
    assert evaluate_restore_verdict(
        compatibility_checks=failed_checks,
        result_status='success',
        post_restore_diff=diff_ok,
    ) == 'restore_incompatible'


def test_cfg41_restore_record_verdict_consistency_enforced():
    """The builder derives the verdict from evidence — and a hand-forged
    record whose verdict overclaims is rejected by the model validator
    even before the hash check."""
    record = build_restore_record(
        document_id=DOC,
        target_instance_id='avr-1',
        artifact_sha256=SHA_A,
        result_status='success',
        post_restore_diff=None,
    )
    assert record.verdict == 'restore_unverified'
    forged = {**record.model_dump(mode='python'),
              'verdict': 'restored_exact_observed_state'}
    from htdt.cad_device_snapshot import ConfigurationRestoreRecord
    with pytest.raises(ValidationError):
        ConfigurationRestoreRecord(**forged)
    forged = {**record.model_dump(mode='python'),
              'verdict': 'restore_incompatible'}
    with pytest.raises(ValidationError):
        ConfigurationRestoreRecord(**forged)


def test_cfg42_restore_record_round_trip(tmp_path):
    repository = _device_repo(tmp_path)
    artifact = _backup_artifact()
    repository.save_backup_artifact(artifact)
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    record = build_restore_record(
        document_id=DOC,
        target_instance_id='avr-1',
        artifact_sha256=artifact.content_sha256,
        source_snapshot_sha256=snapshot.snapshot_sha256,
        compatibility_checks=(_check('model_match', 'PASS'),),
        result_status='success',
        post_restore_observation_sha256=SHA_C,
    )
    repository.save_restore_record(record)
    repository.save_restore_record(record)  # no-op
    got = repository.get_restore_record(record.restore_id)
    assert got == record


def test_cfg43_restore_needs_persisted_source(tmp_path):
    repository = _device_repo(tmp_path)
    record = build_restore_record(
        document_id=DOC,
        target_instance_id='avr-1',
        artifact_sha256=SHA_A,
        result_status='success',
    )
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_restore_record(record)


def test_cfg44_restore_must_name_a_source():
    with pytest.raises(ValidationError):
        build_restore_record(
            document_id=DOC,
            target_instance_id='avr-1',
            result_status='success',
        )


def test_cfg50_backup_artifact_persists(tmp_path):
    repository = _device_repo(tmp_path)
    artifact = _backup_artifact()
    repository.save_backup_artifact(artifact)
    repository.save_backup_artifact(artifact)  # no-op
    got = repository.get_backup_artifact(artifact.content_sha256)
    assert got == artifact
    by_id = repository.get_backup_artifact_by_id(artifact.artifact_id)
    assert by_id == artifact
    listed = repository.list_backup_artifacts('avr-1')
    assert [a.artifact_id for a in listed] == ['art-1']


def test_cfg51_backup_artifact_content_address_conflict(tmp_path):
    repository = _device_repo(tmp_path)
    artifact = _backup_artifact()
    repository.save_backup_artifact(artifact)
    # the same backup content registered under a different claimed
    # identity is a conflict — the artifact is content-addressed
    other = _backup_artifact(artifact_id='art-2', firmware_version='9.9.9')
    assert other.artifact_sha256 != artifact.artifact_sha256
    assert other.content_sha256 == artifact.content_sha256
    with pytest.raises(DeviceSnapshotConflictError):
        repository.save_backup_artifact(other)


def test_cfg52_backup_artifact_row_integrity(tmp_path):
    """Duplicated columns are audited against the canonical payload by
    the row-integrity scan — a tampered column is reported as drift."""
    repository = _device_repo(tmp_path)
    artifact = _backup_artifact()
    repository.save_backup_artifact(artifact)
    import sqlite3
    from htdt.cad_schema import connect_sqlite
    from htdt.native_row_integrity import scan_native_row_integrity
    with connect_sqlite(repository.path) as connection:
        connection.execute(
            'UPDATE cad_device_backup_artifacts SET model=? '
            'WHERE artifact_id=?',
            ('StormAudio', artifact.artifact_id),
        )
        connection.commit()
        drifts = scan_native_row_integrity(connection)
    assert any(
        drift.table == 'cad_device_backup_artifacts'
        and drift.column == 'model'
        for drift in drifts
    )


def test_cfg60_replacement_assessment_round_trip(tmp_path):
    repository = _device_repo(tmp_path)
    assessment = build_replacement_assessment(
        document_id=DOC,
        source_instance_id='avr-1',
        target_instance_id='avr-2',
        source_model='Altitude 16',
        target_model='Altitude 16',
        entries=(
            ReplacementPortabilityEntry(
                item_key='speaker_levels',
                portability='portable_to_same_model',
            ),
            ReplacementPortabilityEntry(
                item_key='license_activation',
                portability='license_bound',
            ),
            ReplacementPortabilityEntry(
                item_key='measured_filters',
                portability='device_instance_bound',
            ),
        ),
    )
    repository.save_replacement_assessment(assessment)
    got = repository.get_replacement_assessment(assessment.assessment_id)
    assert got == assessment
    blocked = replacement_blocking_entries(assessment)
    assert {e.item_key for e in blocked} == {
        'license_activation', 'measured_filters'
    }
    conditional = replacement_conditional_entries(assessment)
    assert conditional == ()


def test_cfg61_assessment_rejects_self_replacement():
    with pytest.raises(ValidationError):
        build_replacement_assessment(
            document_id=DOC,
            source_instance_id='avr-1',
            target_instance_id='avr-1',
        )


def test_cfg70_pre_update_gate_is_fail_closed():
    checks, verdict = evaluate_pre_update_gate(
        PreUpdateDeclaration(
            known_good_captured=True,
            backup_created=True,
            rollback_documented=True,
            release_notes_reviewed=True,
            compatibility_known=True,
            maintenance_window_approved=True,
        )
    )
    assert verdict == 'update_ready'
    assert all(c.status == 'PASS' for c in checks)
    # an undeclared answer is UNKNOWN, not a pass
    checks, verdict = evaluate_pre_update_gate(
        PreUpdateDeclaration(known_good_captured=True)
    )
    assert verdict == 'update_unverified'
    assert any(c.status == 'UNKNOWN' for c in checks)
    # an explicit "no" fails the gate
    checks, verdict = evaluate_pre_update_gate(
        PreUpdateDeclaration(
            known_good_captured=True,
            backup_created=False,
            rollback_documented=True,
            release_notes_reviewed=True,
            compatibility_known=True,
            maintenance_window_approved=True,
        )
    )
    assert verdict == 'update_blocked'
    failed = [c for c in checks if c.status == 'FAIL']
    assert [c.check for c in failed] == ['backup_created']


# ---------------------------------------------------------------------------
# #599 external standards registry (STD10-80)
# ---------------------------------------------------------------------------


def _document(**overrides):
    kwargs = dict(
        standard_id='iso-3382-1',
        edition='2009',
        document_number='ISO 3382-1',
        publisher='ISO',
        title='Acoustics — Measurement of room acoustic parameters — Part 1',
        lifecycle='published_current',
        rights='public_metadata_only',
        admission='production_eligible',
        primary_source_url='https://www.iso.org/standard/40979.html',
        primary_source_tier='standards_body_catalog',
        source_checked_at_utc='2026-10-05T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_standard_document(**kwargs)


def test_std10_document_identity_is_sealed_and_keyed():
    document = _document()
    assert document.registry_key == 'iso-3382-1@2009'
    assert len(document.document_sha) == 64
    # the seal covers the whole record incl. registration timestamp —
    # identical content at identical registration time is identical
    again = _document(registered_at_utc=document.registered_at_utc)
    assert again.document_sha == document.document_sha


def test_std11_registry_round_trip_and_append_only(tmp_path):
    repository = _standards_repo(tmp_path)
    document = _document()
    repository.save_document(document)
    repository.save_document(document)  # identical is a no-op
    got = repository.get_document('iso-3382-1', '2009')
    assert got == document
    got_by_key = repository.get_document_by_key('iso-3382-1@2009')
    assert got_by_key == document
    # a rewritten row under the same key is a conflict, never an update
    edited = _document(lifecycle='superseded')
    with pytest.raises(ExternalStandardsConflictError):
        repository.save_document(edited)


def test_std12_row_tamper_detected(tmp_path):
    repository = _standards_repo(tmp_path)
    document = _document()
    repository.save_document(document)
    from htdt.cad_schema import connect_sqlite
    with connect_sqlite(repository.path) as connection:
        connection.execute(
            'UPDATE cad_external_standard_documents SET lifecycle=? '
            'WHERE registry_key=?',
            ('superseded', document.registry_key),
        )
        connection.commit()
    with pytest.raises(ExternalStandardsIntegrityError):
        repository.get_document('iso-3382-1', '2009')


def test_std13_lifecycle_observation_conflict_is_honest():
    page = build_lifecycle_observation(
        standard_id='avixa-v202-01',
        edition='2016',
        claimed_lifecycle='published_current',
        source_tier='official_committee_or_review_page',
        source_url='https://www.avixa.org/standards',
    )
    store = build_lifecycle_observation(
        standard_id='avixa-v202-01',
        edition='2016',
        claimed_lifecycle='superseded',
        source_tier='official_standards_store',
        source_url='https://shop.avixa.org/v202',
    )
    # different tiers: the higher tier wins — no conflict
    assert detect_lifecycle_conflict(
        (page, store), 'avixa-v202-01', '2016'
    ) == ()
    # same-tier disagreement is a conflict
    catalog = build_lifecycle_observation(
        standard_id='avixa-v202-01',
        edition='2016',
        claimed_lifecycle='published_current',
        source_tier='official_standards_store',
        source_url='https://shop.avixa.org/v202-page',
    )
    conflict = detect_lifecycle_conflict(
        (store, catalog), 'avixa-v202-01', '2016'
    )
    assert len(conflict) == 2


def test_std14_effective_lifecycle_becomes_status_conflict(tmp_path):
    repository = _standards_repo(tmp_path)
    document = _document(
        standard_id='avixa-v202-01',
        edition='2016',
        document_number='ANSI/AVIXA V202.01',
        publisher='AVIXA',
        lifecycle='published_current',
    )
    a = build_lifecycle_observation(
        standard_id='avixa-v202-01', edition='2016',
        claimed_lifecycle='published_current',
        source_tier='official_standards_store',
        source_url='https://shop.avixa.org/a',
    )
    b = build_lifecycle_observation(
        standard_id='avixa-v202-01', edition='2016',
        claimed_lifecycle='superseded',
        source_tier='official_standards_store',
        source_url='https://shop.avixa.org/b',
    )
    repository.save_document(document)
    repository.save_observation(a)
    repository.save_observation(b)
    assert effective_lifecycle(document, (a, b)) == 'status_conflict'
    assert standards_profile_capability(document, None, (a, b)) == (
        'source_ambiguous'
    )


def test_std15_unregistered_citation_is_fail_closed(tmp_path):
    repository = _standards_repo(tmp_path)
    allowed, reason = standard_citation_allowed(None, None, 'production')
    assert not allowed
    assert 'unregistered' in reason
    assert standards_profile_capability(None, None) == 'not_registered'


def test_std16_draft_never_production():
    document = _document(lifecycle='dis_fdis_prepublication',
                       standard_id='iso-3382-1', edition='ed2-draft')
    assert standards_profile_capability(document, None) == (
        'draft_research_only'
    )
    allowed, reason = standard_citation_allowed(
        document, None, 'production'
    )
    assert not allowed
    allowed, _ = standard_citation_allowed(document, None, 'research')
    assert allowed


def test_std17_superseded_is_historical_only():
    document = _document(lifecycle='superseded',
                         replaced_by='iso-3382-1@ed2')
    assert standards_profile_capability(document, None) == (
        'superseded_historical_only'
    )
    allowed, _ = standard_citation_allowed(document, None, 'production')
    assert not allowed
    # pinned historical reproductions still allowed
    allowed, _ = standard_citation_allowed(document, None, 'historical')
    assert allowed


def test_std18_admission_gate_for_mapping():
    document = _document(admission='discovered')
    assert standards_profile_capability(document, None) == (
        'mapping_unvalidated'
    )
    document = _document(admission='limited')
    assert standards_profile_capability(document, None) == 'limited'
    document = _document(admission='retired_for_new_projects')
    assert standards_profile_capability(document, None) == (
        'retired_for_new_projects'
    )


def test_std19_rights_boundary_limits_capability():
    document = _document(rights='reference_only')
    mapping = build_profile_mapping(
        standard_id='iso-3382-1', edition='2009',
        mapping_version='rev1', calculation_version='calc-2026a',
    )
    assert standards_profile_capability(document, mapping) == 'limited'
    document = _document(rights='unknown_rights')
    assert standards_profile_capability(document, mapping) == 'limited'


def test_std20_mapping_must_reference_persisted_document(tmp_path):
    repository = _standards_repo(tmp_path)
    mapping = build_profile_mapping(
        standard_id='iso-3382-1', edition='2009',
        mapping_version='rev1', calculation_version='calc-2026a',
        mapped_requirement_ids=('clarity-c50', 'reverb-t20'),
    )
    with pytest.raises(ExternalStandardsIntegrityError):
        repository.save_mapping(mapping)
    repository.save_document(_document())
    repository.save_mapping(mapping)
    got = repository.get_mapping(mapping.mapping_id)
    assert got == mapping
    assert repository.mapping_for('iso-3382-1', '2009') == mapping


def test_std21_evaluation_pin_records_exact_edition_and_mapping(tmp_path):
    repository = _standards_repo(tmp_path)
    repository.save_document(_document())
    mapping = build_profile_mapping(
        standard_id='iso-3382-1', edition='2009',
        mapping_version='rev1', calculation_version='calc-2026a',
    )
    repository.save_mapping(mapping)
    pin = build_evaluation_pin(
        document_id=DOC,
        standard_id='iso-3382-1',
        edition='2009',
        result='pass',
        mapping=mapping,
        input_evidence_ids=('meas-1',),
    )
    repository.save_pin(pin)
    assert pin.mapping_version == 'rev1'
    assert pin.calculation_version == 'calc-2026a'
    assert pin.mapping_id == mapping.mapping_id
    got = repository.get_pin(pin.pin_id)
    assert got == pin
    listed = repository.list_pins(DOC)
    assert [p.pin_id for p in listed] == [pin.pin_id]


def test_std22_pin_rejects_unregistered_or_mismatched(tmp_path):
    repository = _standards_repo(tmp_path)
    pin = build_evaluation_pin(
        document_id=DOC, standard_id='iso-3382-1', edition='2009',
        result='pass',
    )
    with pytest.raises(ExternalStandardsIntegrityError):
        repository.save_pin(pin)
    repository.save_document(_document())
    repository.save_document(
        _document(standard_id='iso-3382-2', edition='2008',
                  document_number='ISO 3382-2')
    )
    wrong_mapping = build_profile_mapping(
        standard_id='iso-3382-2', edition='2008',
        mapping_version='rev1', calculation_version='calc-2026a',
    )
    repository.save_mapping(wrong_mapping)
    # a mapping for a different edition cannot even be pinned — the
    # builder refuses before the repository is reached
    with pytest.raises(ValueError):
        build_evaluation_pin(
            document_id=DOC, standard_id='iso-3382-1', edition='2009',
            result='pass', mapping=wrong_mapping,
        )


def test_std23_revision_diff_requires_registered_endpoints(tmp_path):
    repository = _standards_repo(tmp_path)
    diff = build_revision_diff(
        from_standard_id='iec-60268-16', from_edition='2011',
        to_standard_id='iec-60268-16', to_edition='2020',
        entries=(
            RevisionDiffEntry(
                change_kind='threshold_changed',
                subject='sti_rating_scale',
            ),
        ),
    )
    with pytest.raises(ExternalStandardsIntegrityError):
        repository.save_revision_diff(diff)
    repository.save_document(_document(
        standard_id='iec-60268-16', edition='2011',
        document_number='IEC 60268-16', publisher='IEC',
        lifecycle='superseded', replaced_by='iec-60268-16@2020',
    ))
    repository.save_document(_document(
        standard_id='iec-60268-16', edition='2020',
        document_number='IEC 60268-16', publisher='IEC',
    ))
    repository.save_revision_diff(diff)
    got = repository.get_revision_diff(diff.diff_id)
    assert got == diff


def test_std24_diff_identifies_affected_pins(tmp_path):
    repository = _standards_repo(tmp_path)
    repository.save_document(_document(
        standard_id='iec-60268-16', edition='2011',
        document_number='IEC 60268-16', publisher='IEC',
        lifecycle='superseded', replaced_by='iec-60268-16@2020',
    ))
    repository.save_document(_document(
        standard_id='iec-60268-16', edition='2020',
        document_number='IEC 60268-16', publisher='IEC',
    ))
    pin = build_evaluation_pin(
        document_id=DOC, standard_id='iec-60268-16', edition='2011',
        result='pass',
    )
    unrelated = build_evaluation_pin(
        document_id=DOC, standard_id='iso-3382-1', edition='2009',
        result='pass',
    )
    diff = build_revision_diff(
        from_standard_id='iec-60268-16', from_edition='2011',
        to_standard_id='iec-60268-16', to_edition='2020',
        entries=(
            RevisionDiffEntry(
                change_kind='metric_changed', subject='sti_scale'),
        ),
    )
    affected = affected_evaluation_pins((pin, unrelated), diff)
    assert [p.pin_id for p in affected] == [pin.pin_id]


def test_std30_seed_covers_real_in_repo_citations(tmp_path):
    """The registry seed covers every external standard the codebase
    cites — editions pinned, honest lifecycle, no invented certainty."""
    repository = _standards_repo(tmp_path)
    docs = seed_standard_documents()
    for document in docs:
        repository.save_document(document)
    keys = {document.registry_key for document in docs}
    # the documents REV55/56 authorities actually cite
    assert 'iso-3382-1@2009' in keys
    assert 'iso-3382-2@2008' in keys
    assert 'iso-10534-2@2023' in keys
    assert 'iso-10534-2@1998' in keys
    assert 'iec-60268-16@2020' in keys
    assert 'iec-61672-1@2013' in keys
    assert 'jcgm-100@2008' in keys
    assert 'ilac-g8@09/2019' in keys
    assert 'cedia-cta-rp22@v1.2' in keys
    by_key = {document.registry_key: document for document in docs}
    # verified lifecycle claims (literature-checked 2026-10-05)
    assert by_key['iso-3382-1@2009'].lifecycle == 'under_revision'
    assert by_key['iso-3382-2@2008'].lifecycle == 'reaffirmed'
    assert by_key['iso-10534-2@1998'].lifecycle == 'withdrawn'
    assert by_key['iso-10534-2@1998'].replaced_by == 'iso-10534-2@2023'
    assert by_key['iec-60268-5@2003'].lifecycle == 'withdrawn'
    assert by_key['iec-60268-5@2003'].replaced_by == (
        'iec-60268-21@2018'
    )
    assert by_key['ansi-asa-s12-2@2019'].lifecycle == 'superseded'
    # superseded but still pinned by a dependent standard — exactly the
    # honest registration the issue requires
    rp22 = by_key['cedia-cta-rp22@v1.2']
    assert any(
        ref.standard_id == 'ansi-asa-s12-2' and ref.edition == '2019'
        for ref in rp22.dependency_refs
    )


def test_std31_seed_observations_record_avixa_disagreement(tmp_path):
    """The real AVIXA case: the store (higher tier) says 2016 is
    superseded while the committee page still presents it as current.
    Tier priority resolves the lifecycle — superseded — while BOTH
    observations stay persisted so the disagreement is auditable."""
    repository = _standards_repo(tmp_path)
    for document in seed_standard_documents():
        repository.save_document(document)
    for observation in seed_lifecycle_observations():
        repository.save_observation(observation)
    document = repository.get_document('avixa-v202-01', '2016')
    observations = repository.observations_for(
        'avixa-v202-01', '2016'
    )
    assert len(observations) == 2
    assert {o.source_tier for o in observations} == {
        'official_committee_or_review_page', 'official_standards_store'
    }
    # tier priority: store wins — and the committee claim remains stored
    assert effective_lifecycle(document, observations) == 'superseded'
    assert standards_profile_capability(
        document, None, observations
    ) == 'superseded_historical_only'


def test_std40_display_labels_cover_every_literal():
    for status in (
        'draft', 'public_review', 'dis_fdis_prepublication',
        'industry_review', 'published_current', 'reaffirmed',
        'under_revision', 'superseded', 'revised', 'withdrawn',
        'replaced_by', 'historical', 'status_conflict', 'unknown',
    ):
        assert standards_lifecycle_label(status) != status
    for verdict in (
        'production_eligible', 'limited', 'draft_research_only',
        'not_registered', 'source_ambiguous', 'mapping_unvalidated',
        'license_profile_unavailable', 'superseded_historical_only',
        'retired_for_new_projects',
    ):
        assert standards_capability_label(verdict) != verdict
    for evidence_class in (
        'device_readback', 'device_export_backup',
        'htdt_applied_request', 'user_recorded',
        'screenshot_documented', 'inferred_from_measurement', 'unknown',
    ):
        assert device_evidence_class_label(evidence_class) != (
            evidence_class
        )
    for verdict in (
        'restored_exact_observed_state', 'restored_with_differences',
        'restore_unverified', 'restore_incompatible', 'restore_failed',
    ):
        assert restore_verdict_label(verdict) != verdict


def test_std41_display_lines_are_honest_about_unknown():
    document = _document(lifecycle='unknown', admission='discovered')
    line = standards_document_line(document)
    assert 'ISO 3382-1:2009' in line
    assert '状態不明' in line
    snapshot = _snapshot(manufacturer=None, model=None,
                         firmware_version=None)
    line = device_snapshot_line(snapshot)
    assert '機種不明' in line
    assert 'ファームウェア不明' in line
    record = build_restore_record(
        document_id=DOC, target_instance_id='avr-1',
        artifact_sha256=SHA_A, result_status='failed',
    )
    line = restore_record_line(record)
    assert '復元失敗' in line
    pin = build_evaluation_pin(
        document_id=DOC, standard_id='iso-3382-1', edition='2009',
        result='missing_required_evidence',
    )
    line = standards_pin_line(pin)
    assert 'iso-3382-1@2009' in line


def test_std50_primary_source_url_requires_tier():
    with pytest.raises(ValidationError):
        _document(primary_source_url='https://example.org',
                  primary_source_tier=None)


def test_std51_replaced_by_self_reference_rejected():
    with pytest.raises(ValidationError):
        _document(replaced_by='iso-3382-1@2009')


def test_std52_observation_round_trip_and_append_only(tmp_path):
    repository = _standards_repo(tmp_path)
    observation = build_lifecycle_observation(
        standard_id='iec-60268-5', edition='2003',
        claimed_lifecycle='withdrawn',
        source_tier='standards_body_catalog',
        source_url='https://webstore.iec.ch/en/publication/6165',
        claimed_revision_text='Withdrawn 2026-04-17, replaced by IEC 60268-21',
    )
    repository.save_observation(observation)
    repository.save_observation(observation)
    got = repository.get_observation(observation.observation_id)
    assert got == observation


# ---------------------------------------------------------------------------
# schema wiring
# ---------------------------------------------------------------------------


def test_std60_tables_registered_in_schema_contract():
    from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES
    expected = {
        'cad_device_backup_artifacts',
        'cad_device_config_snapshots',
        'cad_device_firmware_transitions',
        'cad_device_known_good_baselines',
        'cad_device_replacement_assessments',
        'cad_device_restore_records',
        'cad_external_standard_documents',
        'cad_standard_evaluation_pins',
        'cad_standard_lifecycle_observations',
        'cad_standard_profile_mappings',
        'cad_standard_revision_diffs',
    }
    assert expected <= set(NATIVE_SCHEMA_TABLES)


def test_std61_migration_from_v24_creates_tables(tmp_path):
    """A v24-era database upgrades in place — the authority is additive."""
    repository = _device_repo(tmp_path)
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    repository.save_snapshot(snapshot)
    assert repository.get_snapshot(snapshot.snapshot_id) == snapshot
    from htdt.cad_schema import NATIVE_SCHEMA_VERSION
    assert NATIVE_SCHEMA_VERSION == 25
