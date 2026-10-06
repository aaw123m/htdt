"""REV59-DEPS regression tests — #729 authority dependency / staleness
graph, #725 evidence attestation / trusted timestamp authority, #718
project archival / schema-migration authority.

Fixtures (DEP/ATT/ARC) exercise the fail-closed rules the issues
demand: only declared dependencies invalidate, a hash alone claims no
signer and no time, and an unverified migration never claims
preservation.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_authority_dependency import (
    DependencyRuleEntry,
    DependencyScope,
    build_change_event,
    build_dependency_edge,
    build_revalidation_plan,
    build_rule_profile,
    dependency_edge_binding,
    evaluate_staleness,
)
from htdt.cad_authority_dependency_repository import (
    CadAuthorityDependencyRepository,
    DependencyGraphConflictError,
    DependencyGraphIntegrityError,
)
from htdt.cad_evidence_attestation import (
    CanonicalizationSpec,
    SignatureDescriptor,
    TrustedTimestampPin,
    VerificationInputs,
    attestation_binding,
    build_attestation,
    build_signed_manifest,
    evaluate_attestation,
    manifest_binding,
)
from htdt.cad_evidence_attestation_repository import (
    AttestationConflictError,
    AttestationIntegrityError,
    CadEvidenceAttestationRepository,
)
from htdt.cad_archive_migration import (
    ArchiveCheckResult,
    FieldChangeIntent,
    MigrationCheckResult,
    ProcedurePin,
    archive_binding,
    build_archive_snapshot,
    build_migration_record,
    evaluate_archive,
    evaluate_migration,
    migration_binding,
)
from htdt.cad_archive_migration_repository import (
    ArchiveMigrationConflictError,
    ArchiveMigrationIntegrityError,
    CadArchiveMigrationRepository,
)


DOC = 'doc-rev59-deps'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
T2 = '2026-10-06T02:00:00+00:00'
T3 = '2026-10-06T03:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64
SHA_F = 'f' * 64

EVIDENCE_REF = AuthorityRef(
    kind='impulse_response_measurement',
    ref_id='rir-1',
    ref_sha256=SHA_A,
)
PROFILE_REF = AuthorityRef(
    kind='acoustic_target_profile', ref_id='profile-1', ref_sha256=SHA_B
)
VERDICT_REF = AuthorityRef(
    kind='decision_verdict', ref_id='verdict-1', ref_sha256=SHA_C
)
UNRELATED_REF = AuthorityRef(
    kind='report_artifact', ref_id='report-1', ref_sha256=SHA_D
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _tamper(db_path, sql: str, params=()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #729 — authority dependency / staleness graph (DEP fixtures)
# ---------------------------------------------------------------------------


def _dep_edges():
    verdict_edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='derived_from',
        target_ref=EVIDENCE_REF,
        declared_at_utc=T0,
    )
    evidence_edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=EVIDENCE_REF,
        kind='measured_under_state',
        target_ref=PROFILE_REF,
        declared_at_utc=T0,
    )
    return verdict_edge, evidence_edge


def _dep_rules():
    return build_rule_profile(
        document_id=DOC,
        ruleset_version='rs-1',
        entries=(
            DependencyRuleEntry(
                entry_id='rule-derived-calib',
                edge_kind='derived_from',
                change_class='calibration_parameters',
                effect='recompute',
            ),
            DependencyRuleEntry(
                entry_id='rule-state-calib',
                edge_kind='measured_under_state',
                change_class='calibration_parameters',
                effect='remeasure',
                action='requalify_profile',
            ),
            DependencyRuleEntry(
                entry_id='rule-derived-label',
                edge_kind='derived_from',
                change_class='label_metadata',
                effect='none',
            ),
        ),
        declared_at_utc=T0,
    )


def _dep_event(change_class='calibration_parameters', fields=('method',)):
    return build_change_event(
        document_id=DOC,
        changed_ref=PROFILE_REF,
        change_class=change_class,
        changed_fields=fields,
        occurred_at_utc=T1,
    )


def test_dep10_precise_invalidation_scope(tmp_path):
    """DEP10: a profile change stales exactly the declared dependents —
    the verdict through the evidence chain, nothing else."""
    edges = _dep_edges()
    assessment = evaluate_staleness(
        DOC, edges, _dep_event(), _dep_rules(), evaluated_at_utc=T2
    )
    states = {e.subject_ref.ref_id: e.state for e in assessment.entries}
    # The profile's dependent evidence stales at remeasure; the verdict
    # inherits the strongest limitation on its path (§11).
    assert states[EVIDENCE_REF.ref_id] == 'stale_remeasure'
    assert states[VERDICT_REF.ref_id] == 'stale_remeasure'
    assert UNRELATED_REF.ref_id not in states


def test_dep20_transitive_closure_path_reported(tmp_path):
    """DEP20: transitive paths are recorded on the verdict entry."""
    edges = _dep_edges()
    assessment = evaluate_staleness(
        DOC, edges, _dep_event(), _dep_rules(), evaluated_at_utc=T2
    )
    verdict_entry = next(
        e
        for e in assessment.entries
        if e.subject_ref.ref_id == VERDICT_REF.ref_id
    )
    path_ids = [r.ref_id for r in verdict_entry.path]
    assert path_ids[0] == VERDICT_REF.ref_id
    assert path_ids[-1] == PROFILE_REF.ref_id
    assert EVIDENCE_REF.ref_id in path_ids


def test_dep30_references_only_never_propagates(tmp_path):
    """DEP30: a references_only edge is inspection metadata — a change
    never invalidates through it."""
    ref_edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='references_only',
        target_ref=PROFILE_REF,
        declared_at_utc=T0,
    )
    assessment = evaluate_staleness(
        DOC, (ref_edge,), _dep_event(), _dep_rules(), evaluated_at_utc=T2
    )
    assert assessment.entries == ()


def test_dep40_scope_gated_edge_does_not_fire(tmp_path):
    """DEP40: an edge scoped to fields outside the change stays quiet —
    a speaker label rename does not stale a geometry dependency."""
    scoped_edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='derived_from',
        target_ref=PROFILE_REF,
        scope=DependencyScope(target_fields=('geometry',)),
        declared_at_utc=T0,
    )
    assessment = evaluate_staleness(
        DOC,
        (scoped_edge,),
        _dep_event(fields=('label',)),
        _dep_rules(),
        evaluated_at_utc=T2,
    )
    assert assessment.entries == ()


def test_dep50_undeclared_subjects_fail_closed(tmp_path):
    """DEP50: authorities with no declared dependency coverage surface
    as unknown_dependency — they never silently stay current."""
    assessment = evaluate_staleness(
        DOC,
        _dep_edges(),
        _dep_event(),
        _dep_rules(),
        unknown_subjects=(UNRELATED_REF,),
        evaluated_at_utc=T2,
    )
    unknown = next(
        e
        for e in assessment.entries
        if e.subject_ref.ref_id == UNRELATED_REF.ref_id
    )
    assert unknown.state == 'unknown_dependency'
    assert unknown.conservative


def test_dep60_no_matching_rule_is_conservative_review(tmp_path):
    """DEP60: a firing edge with no ruleset entry cannot be ruled
    safe — it degrades to stale_review_required, marked conservative."""
    edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='validated_against',
        target_ref=PROFILE_REF,
        declared_at_utc=T0,
    )
    assessment = evaluate_staleness(
        DOC, (edge,), _dep_event(), _dep_rules(), evaluated_at_utc=T2
    )
    entry = next(iter(assessment.entries))
    assert entry.state == 'stale_review_required'
    assert entry.conservative
    assert entry.required_action == 'review_evidence'


def test_dep70_revalidation_plan_minimal_scope(tmp_path):
    """DEP70: the plan emits the smallest work set — each subject once,
    under the action its winning rule declared."""
    edges = _dep_edges()
    assessment = evaluate_staleness(
        DOC, edges, _dep_event(), _dep_rules(), evaluated_at_utc=T2
    )
    plan = build_revalidation_plan(
        DOC, assessment, planned_at_utc=T3
    )
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.kind == 'requalify_profile'
    assert {r.ref_id for r in action.subject_refs} == {
        EVIDENCE_REF.ref_id,
        VERDICT_REF.ref_id,
    }


def test_dep80_repository_round_trip_and_tamper(tmp_path):
    """DEP80: sealed records persist verbatim; a tampered mirrored
    column fails integrity on read."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadAuthorityDependencyRepository(scene_repository)
    edges = _dep_edges()
    event = _dep_event()
    rules = _dep_rules()
    for edge in edges:
        repo.save_edge(edge)
    repo.save_change_event(event)
    repo.save_rule_profile(rules)

    assessment = evaluate_staleness(
        DOC, edges, event, rules, evaluated_at_utc=T2
    )
    repo.save_assessment(assessment)
    plan = build_revalidation_plan(DOC, assessment, planned_at_utc=T3)
    repo.save_plan(plan)

    assert repo.get_edge(edges[0].edge_id) == edges[0]
    assert repo.get_change_event(event.event_id) == event
    assert repo.get_rule_profile(rules.profile_id) == rules
    assert repo.get_assessment(assessment.assessment_id) == assessment
    assert repo.get_plan(plan.plan_id) == plan

    # Idempotent identical save.
    repo.save_edge(edges[0])

    _tamper(
        repo.path,
        'UPDATE cad_dependency_edge_declarations SET kind=? '
        'WHERE edge_id=?',
        ('supersedes', edges[0].edge_id),
    )
    with pytest.raises(DependencyGraphIntegrityError):
        repo.get_edge(edges[0].edge_id)


def test_dep85_append_only_conflict(tmp_path):
    """DEP85: same id with a different payload is a conflict, never a
    silent update."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadAuthorityDependencyRepository(scene_repository)
    edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='derived_from',
        target_ref=EVIDENCE_REF,
        declared_at_utc=T0,
    )
    repo.save_edge(edge)
    forged = edge.model_copy(update={'kind': 'validated_against'})
    with pytest.raises(Exception):
        repo.save_edge(forged)


def test_dep90_label_metadata_change_stays_quiet(tmp_path):
    """DEP90: a label_metadata event under a 'none' rule does not stale
    the dependent — not every change is an invalidation."""
    edges = _dep_edges()[:1]  # verdict derived_from evidence only
    event = build_change_event(
        document_id=DOC,
        changed_ref=EVIDENCE_REF,
        change_class='label_metadata',
        changed_fields=('label',),
        occurred_at_utc=T1,
    )
    assessment = evaluate_staleness(
        DOC, edges, event, _dep_rules(), evaluated_at_utc=T2
    )
    assert assessment.entries == ()


def test_dep95_sealed_record_rejects_tampered_payload():
    """DEP95: any field drifted from the sealed identity fails parse."""
    edge = build_dependency_edge(
        document_id=DOC,
        subject_ref=VERDICT_REF,
        kind='derived_from',
        target_ref=EVIDENCE_REF,
        declared_at_utc=T0,
    )
    payload = edge.model_dump()
    payload['kind'] = 'supersedes'
    with pytest.raises(Exception):
        type(edge).model_validate(payload)


# ---------------------------------------------------------------------------
# #725 — evidence attestation / trusted timestamp authority (ATT fixtures)
# ---------------------------------------------------------------------------


def _canon():
    return CanonicalizationSpec(
        serialization_format='htdt-canonical-json',
        canonicalization_id='canonical-json',
        canonicalization_version='1',
    )


def _manifest(label='release-manifest'):
    return build_signed_manifest(
        document_id=DOC,
        manifest_label=label,
        manifest_sha256=SHA_D,
        canonicalization=_canon(),
        approval_scope='verification_result',
        subject_refs=(EVIDENCE_REF, VERDICT_REF),
        referenced_artifacts=(SHA_E,),
        created_at_utc=T0,
    )


def _signature():
    return SignatureDescriptor(
        algorithm='ed25519',
        signature_format='jws_compact',
        key_id='key-ops-1',
        signer_type='human',
        signer_label='M. Engineer',
        signature_sha256=SHA_E,
        creation_time_claim_utc=T1,
    )


def _timestamp():
    return TrustedTimestampPin(
        tsa_identity='tsa.example.test',
        token_sha256=SHA_F,
        imprint_algorithm='sha256',
        claimed_time_utc=T1,
    )


def _attest(kind, manifest=None):
    manifest = manifest or _manifest()
    return manifest, build_attestation(
        document_id=DOC,
        manifest_ref=manifest_binding(manifest),
        kind=kind,
        signature=(
            _signature()
            if kind
            in (
                'digitally_signed',
                'digitally_signed_with_certificate_chain',
                'signed_and_trusted_timestamped',
                'mac_authenticated',
            )
            else None
        ),
        trusted_timestamp=(
            _timestamp()
            if kind
            in ('trusted_timestamp_only', 'signed_and_trusted_timestamped')
            else None
        ),
        declared_at_utc=T1,
    )


def _inputs(**overrides):
    base = dict(
        manifest_hash_match=True,
        signature_check='verified',
        timestamp_check='verified',
        key_state_at_signing_time='valid',
        verification_policy_id='att-policy',
        verification_policy_version='1.0',
    )
    base.update(overrides)
    return VerificationInputs(**base)


def test_att10_signed_and_timestamped_verifies():
    """ATT10: verified signature + verified trusted timestamp +
    valid key at signing time → attested_verified, trusted-time."""
    manifest, attestation = _attest('signed_and_trusted_timestamped')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'attested_verified'
    assert verdict.time_authority == 'trusted_timestamp'


def test_att20_hash_only_confirms_integrity_only():
    """ATT20: a hash-only bundle can prove integrity — never a signer
    and never a time."""
    manifest, attestation = _attest('hash_only')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'integrity_confirmed'
    assert verdict.time_authority == 'unknown_time'


def test_att30_unchecked_signature_is_unprovened():
    """ATT30: a signature that was never verified is
    attested_unprovened — never silently verified."""
    manifest, attestation = _attest('digitally_signed')
    verdict = evaluate_attestation(
        DOC,
        manifest,
        attestation,
        _inputs(signature_check='not_checked'),
        evaluated_at_utc=T2,
    )
    assert verdict.state == 'attested_unprovened'


def test_att40_declared_time_only():
    """ATT40: a verified signature with no trusted timestamp claims at
    most the signer's declared time."""
    manifest, attestation = _attest('digitally_signed')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'attested_verified_declared_time'
    assert verdict.time_authority == 'declared_time'


def test_att50_historical_signature_under_trusted_time():
    """ATT50: a since-expired key still verifies historically when a
    trusted timestamp anchors the signing time."""
    manifest, attestation = _attest('signed_and_trusted_timestamped')
    verdict = evaluate_attestation(
        DOC,
        manifest,
        attestation,
        _inputs(key_state_at_signing_time='expired'),
        evaluated_at_utc=T2,
    )
    assert verdict.state == 'attested_verified_historical'
    assert verdict.time_authority == 'trusted_timestamp'


def test_att60_manifest_mismatch_fails():
    """ATT60: an attestation pinned to a different manifest fails —
    a redacted artifact never inherits the original signature."""
    manifest, _ = _attest('digitally_signed')
    other_manifest = _manifest(label='redacted-copy')
    mismatch = build_attestation(
        document_id=DOC,
        manifest_ref=manifest_binding(other_manifest),
        kind='digitally_signed',
        signature=_signature(),
        declared_at_utc=T1,
    )
    verdict = evaluate_attestation(
        DOC, manifest, mismatch, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'verification_failed'


def test_att70_trusted_timestamp_only():
    """ATT70: a verified TSA token alone confirms existence-before-time
    — no signer claim."""
    manifest, attestation = _attest('trusted_timestamp_only')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'timestamp_confirmed'
    assert verdict.time_authority == 'trusted_timestamp'


def test_att75_external_reference_unverifiable():
    """ATT75: an imported signature reference the authority cannot
    resolve is unverifiable, not passed."""
    manifest, attestation = _attest('external_signature_reference')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    assert verdict.state == 'unverifiable'


def test_att80_attestation_chain():
    """ATT80: attestation-of-attestation chaining preserves
    provenance — the parent pin is part of the sealed identity."""
    manifest, parent = _attest('digitally_signed')
    chained = build_attestation(
        document_id=DOC,
        manifest_ref=manifest_binding(manifest),
        kind='digitally_signed',
        signature=_signature(),
        attests_attestation_ref=attestation_binding(parent),
        declared_at_utc=T2,
    )
    assert chained.attests_attestation_ref is not None
    assert (
        chained.attests_attestation_ref.ref_id
        == parent.attestation_id
    )


def test_att85_repository_round_trip_and_tamper(tmp_path):
    """ATT85: sealed manifests/attestations/verdicts persist verbatim;
    tampered columns fail integrity on read."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadEvidenceAttestationRepository(scene_repository)
    manifest, attestation = _attest('signed_and_trusted_timestamped')
    verdict = evaluate_attestation(
        DOC, manifest, attestation, _inputs(), evaluated_at_utc=T2
    )
    repo.save_manifest(manifest)
    repo.save_attestation(attestation)
    repo.save_verification(verdict)

    assert repo.get_manifest(manifest.manifest_id) == manifest
    assert repo.get_attestation(attestation.attestation_id) == attestation
    assert repo.get_verification(verdict.verification_id) == verdict

    _tamper(
        repo.path,
        'UPDATE cad_manifest_attestations SET kind=? '
        'WHERE attestation_id=?',
        ('hash_only', attestation.attestation_id),
    )
    with pytest.raises(AttestationIntegrityError):
        repo.get_attestation(attestation.attestation_id)


def test_att90_unsupported_digest_policy_fails_closed():
    """ATT90: an unsupported hash algorithm under the pinned policy
    fails closed."""
    manifest, attestation = _attest('digitally_signed')
    verdict = evaluate_attestation(
        DOC,
        manifest,
        attestation,
        _inputs(hash_algorithm_policy='unsupported'),
        evaluated_at_utc=T2,
    )
    assert verdict.state == 'verification_failed'


# ---------------------------------------------------------------------------
# #718 — project archival / schema-migration authority (ARC fixtures)
# ---------------------------------------------------------------------------


def _proc(version='1.0'):
    return ProcedurePin(
        procedure_id='htdt-native-export', procedure_version=version
    )


def _archive(label='arc-48', schema_version='48', content=SHA_A,
             checksum=SHA_B):
    return build_archive_snapshot(
        document_id=DOC,
        archive_label=label,
        schema_version=schema_version,
        content_hash=content,
        snapshot_semantic_checksum=checksum,
        preservation_scope='full_semantics_and_provenance',
        write_procedure=_proc(),
        declared_readback_procedure=_proc(),
        captured_at_utc=T0,
    )


def _archive_checks(**overrides):
    outcomes = dict(
        container_legible='pass',
        content_hash_match='pass',
        semantic_checksum_match='pass',
        schema_version_legible='pass',
    )
    outcomes.update(overrides)
    return tuple(
        ArchiveCheckResult(check=k, outcome=v)
        for k, v in outcomes.items()
    )


def test_arc10_legible_archive_verifies():
    """ARC10: all re-read checks pass → verified_legible."""
    archive = _archive()
    verdict = evaluate_archive(
        DOC,
        archive,
        _archive_checks(),
        verification_policy_id='arc-policy',
        verification_policy_version='1.0',
        verified_at_utc=T1,
    )
    assert verdict.status == 'verified_legible'


def test_arc20_unchecked_required_is_partial():
    """ARC20: a required check that never ran is partial verification,
    never a silent pass."""
    verdict = evaluate_archive(
        DOC,
        _archive(),
        _archive_checks(semantic_checksum_match='not_checked'),
        verification_policy_id='arc-policy',
        verification_policy_version='1.0',
        verified_at_utc=T1,
    )
    assert verdict.status == 'partially_verified'


def test_arc30_failed_check_fails():
    """ARC30: any failed re-read check fails the archive."""
    verdict = evaluate_archive(
        DOC,
        _archive(),
        _archive_checks(content_hash_match='fail'),
        verification_policy_id='arc-policy',
        verification_policy_version='1.0',
        verified_at_utc=T1,
    )
    assert verdict.status == 'verification_failed'


def test_arc40_empty_checks_unverified():
    """ARC40: declared legibility alone is unverified."""
    verdict = evaluate_archive(
        DOC,
        _archive(),
        (),
        verification_policy_id='arc-policy',
        verification_policy_version='1.0',
        verified_at_utc=T1,
    )
    assert verdict.status == 'unverified'


def _migration():
    source = _archive(label='arc-48', schema_version='48')
    target = _archive(
        label='arc-49', schema_version='49',
        content=SHA_C, checksum=SHA_D,
    )
    record = build_migration_record(
        document_id=DOC,
        kind='schema_upgrade',
        source_archive_ref=archive_binding(source),
        target_archive_ref=archive_binding(target),
        from_schema_version='48',
        to_schema_version='49',
        migration_procedure=_proc('2.0'),
        comparison_policy_id='semantic-cmp',
        comparison_policy_version='1.0',
        declared_field_changes=(
            FieldChangeIntent(
                field_path='surface.material',
                change_kind='renamed',
                justification='v49 renames absorption field',
            ),
        ),
        migrated_at_utc=T1,
    )
    return record


def _migration_checks(**overrides):
    outcomes = dict(
        target_readable='pass',
        content_hash_match='pass',
        semantic_checksum_match='pass',
        referenced_pins_resolve='pass',
        preservation_scope_complete='pass',
        declared_transformations_applied='pass',
    )
    outcomes.update(overrides)
    return tuple(
        MigrationCheckResult(check=k, outcome=v)
        for k, v in outcomes.items()
    )


def test_arc50_equivalent_migration_verifies():
    """ARC50: all required checks pass with a matching semantic
    checksum → verified_equivalent."""
    verdict = evaluate_migration(
        DOC, _migration(), _migration_checks(), verified_at_utc=T2
    )
    assert verdict.status == 'verified_equivalent'


def test_arc60_declared_differences_verify():
    """ARC60: a semantic drift that was declared and accepted is
    verified_with_declared_differences — not equivalence. A checksum
    mismatch *explained* by the declared field changes does not fail
    the migration; an unexplained one does."""
    verdict = evaluate_migration(
        DOC,
        _migration(),
        _migration_checks(
            semantic_checksum_match='fail',
            semantic_diff_declared='pass',
            semantic_diff_accepted='pass',
        ),
        verified_at_utc=T2,
    )
    assert verdict.status == 'verified_with_declared_differences'

    unexplained = evaluate_migration(
        DOC,
        _migration(),
        _migration_checks(semantic_checksum_match='fail'),
        verified_at_utc=T2,
    )
    assert unexplained.status == 'verification_failed'


def test_arc70_missing_required_check_unverified():
    """ARC70: a required check that never ran means the migration
    cannot claim preservation."""
    verdict = evaluate_migration(
        DOC,
        _migration(),
        _migration_checks(preservation_scope_complete='not_checked'),
        verified_at_utc=T2,
    )
    assert verdict.status == 'unverified'


def test_arc75_migration_record_is_declared_at_write():
    """ARC75: a migration record is written 'declared' — verification
    is a separate sealed verdict, never a silent upgrade."""
    record = _migration()
    assert record.migration_status_at_write == 'declared'


def test_arc80_repository_round_trip_and_tamper(tmp_path):
    """ARC80: sealed archives/migrations persist verbatim; tampered
    mirrored columns fail integrity on read."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadArchiveMigrationRepository(scene_repository)
    archive = _archive()
    repo.save_snapshot(archive)
    repo.save_archive_verification(
        evaluate_archive(
            DOC,
            archive,
            _archive_checks(),
            verification_policy_id='arc-policy',
            verification_policy_version='1.0',
            verified_at_utc=T1,
        )
    )
    migration = _migration()
    repo.save_migration(migration)
    verdict = evaluate_migration(
        DOC, migration, _migration_checks(), verified_at_utc=T2
    )
    repo.save_migration_verification(verdict)

    assert repo.get_snapshot(archive.archive_id) == archive
    assert repo.get_migration(migration.migration_id) == migration
    assert (
        repo.get_migration_verification(verdict.verification_id)
        == verdict
    )

    _tamper(
        repo.path,
        'UPDATE cad_migration_records SET to_schema_version=? '
        'WHERE migration_id=?',
        ('99', migration.migration_id),
    )
    with pytest.raises(ArchiveMigrationIntegrityError):
        repo.get_migration(migration.migration_id)


def test_arc85_append_only_conflict(tmp_path):
    """ARC85: same archive id with a different payload is a conflict,
    never a silent update."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadArchiveMigrationRepository(scene_repository)
    archive = _archive()
    repo.save_snapshot(archive)
    forged = archive.model_copy(
        update={'preservation_scope': 'project_structure_only'}
    )
    with pytest.raises(Exception):
        repo.save_snapshot(forged)
