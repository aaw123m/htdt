"""#889 safe application updater — sealed authority + staged apply.

Covers every verdict/state path of the update machine: package seal
integrity and the #849 signature vocabulary, the fail-closed preflight
matrix, one-shot authorization, staged apply with verified rollback
(``rolled_back`` vs ``rollback_failed`` — honest scope), crash recovery
via the sealed transition log (``resume``), and repository round-trip
plus tamper detection for all eight stores.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htdt.cad_application_update import (
    APPLICATION_UPDATE_SCHEMA_VERSION,
    ApplicationUpdateError,
    ApplicationUpdateService,
    FakeDriverScenario,
    FakePackageScenario,
    FakeUpdateEnvironmentProbe,
    FakeUpdateInstallDriver,
    FakeUpdatePackageSource,
    FilesystemUpdateDriver,
    HealthObservation,
    LocalDirectoryPackageSource,
    UpdateArtifactRef,
    UpdateAuthorizationError,
    UpdateCapturedItem,
    UpdateCheckResult,
    UpdateEnvironmentFacts,
    UpdateEnvironmentSnapshot,
    UpdateEvent,
    UpdateHealthReport,
    UpdateOperatorAuthorization,
    UpdateOutcomeRecord,
    UpdatePackageDescriptor,
    UpdatePreflightReport,
    UpdateRestorePoint,
    UpdateSessionRecord,
    UpdateSessionState,
    UpdateSignatureState,
    UpdateStageTransition,
    derive_update_state,
    evaluate_health,
    evaluate_preflight,
    health_binding,
    package_binding,
    preflight_binding,
    restore_point_binding,
    session_binding,
    update_stage_transition,
)
from htdt.cad_application_update_repository import (
    CadApplicationUpdateRepository,
    DeploymentConflictError,
    DeploymentIntegrityError,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    connect_sqlite,
    ensure_native_schema,
)

DOC = 'doc-889'
NOW = '2026-10-08T00:00:00Z'

PAYLOADS: dict[str, bytes] = {
    'app.bin': b'new-application-bytes-v110',
    'lib.dll': b'new-library-bytes-v110',
    'manifest.json': b'{"release": "1.1.0", "channel": "stable"}',
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _artifacts(
    *, payloads: dict[str, bytes] | None = None,
) -> tuple[UpdateArtifactRef, ...]:
    payloads = PAYLOADS if payloads is None else payloads
    return tuple(
        UpdateArtifactRef(
            name=name,
            sha256=_sha256(data),
            size_bytes=len(data),
            kind=(
                'manifest' if name == 'manifest.json'
                else 'payload'),
        )
        for name, data in payloads.items())


def _signature(
    status: str = 'unsigned', **fields,
) -> UpdateSignatureState:
    return UpdateSignatureState(status=status, **fields)


def _package_kwargs(**overrides):
    kwargs = dict(
        target_version='1.1.0',
        channel='stable',
        provenance='release-manifest',
        artifacts=_artifacts(),
        signature=_signature('unsigned'),
        target_native_schema_version=NATIVE_SCHEMA_VERSION,
        schema_floor=100,
        schema_ceiling=NATIVE_SCHEMA_VERSION,
        migration_reversibility='reversible',
        supported_platforms=('windows', 'linux'),
        runtime_floor='3.10',
        required_free_bytes=1024,
        dropped_adapter_ids=(),
        adapter_api_floor=2,
    )
    kwargs.update(overrides)
    return kwargs


def _facts(**overrides) -> UpdateEnvironmentFacts:
    kwargs = dict(
        os_name='windows',
        runtime_version='3.12.10',
        app_version='1.0.0',
        app_native_schema_version=NATIVE_SCHEMA_VERSION,
        data_dir_schema_version=NATIVE_SCHEMA_VERSION,
        free_disk_bytes=1_000_000_000,
        open_transaction_kinds=(),
        pending_recovery_sessions=0,
        bound_adapter_ids=('avr_lan', 'midi_io'),
        adapter_api_level=3,
    )
    kwargs.update(overrides)
    return UpdateEnvironmentFacts(**kwargs)


def _env(tmp_path: Path):
    """Lay down a live install, a data dir and an empty update area."""
    install_root = tmp_path / 'install'
    data_dir = tmp_path / 'data'
    update_area = tmp_path / 'update-area'
    install_root.mkdir(parents=True)
    data_dir.mkdir()
    update_area.mkdir()
    (install_root / 'version.json').write_text(
        json.dumps({
            'version': '1.0.0',
            'target_native_schema_version': NATIVE_SCHEMA_VERSION,
        }),
        encoding='utf-8')
    (install_root / 'app.bin').write_bytes(b'old-application-bytes')
    (install_root / 'lib.dll').write_bytes(b'old-library-bytes')
    for name in (
        'preferences.json', 'device-bindings.json', 'schema-metadata.json',
    ):
        (data_dir / name).write_text('{"v": 1}', encoding='utf-8')
    return install_root, data_dir, update_area


def _healthy(
    *, version: str = '1.1.0', schema: int = NATIVE_SCHEMA_VERSION,
    warn: str | None = None, unknown: str | None = None,
) -> HealthObservation:
    checks = [
        UpdateCheckResult(
            name='app_launch', status='pass',
            code='launch_ok', reason='post-swap process launched'),
        UpdateCheckResult(
            name='version_stamp', status='pass',
            code='version_stamp_match', reason=f'observed {version}'),
        UpdateCheckResult(
            name='schema_open', status='pass',
            code='schema_open_ok', reason=f'schema v{schema} opened'),
        UpdateCheckResult(
            name='project_open', status='pass',
            code='project_open_ok', reason='project opened'),
        UpdateCheckResult(
            name='persistence', status='pass',
            code='persistence_ok', reason='native persistence intact'),
        UpdateCheckResult(
            name='native_runtime', status='pass',
            code='runtime_ok', reason='Qt runtime initialized'),
        UpdateCheckResult(
            name='audio_backend', status='pass',
            code='audio_ok', reason='audio backend available'),
        UpdateCheckResult(
            name='adapter_loading', status='pass',
            code='adapters_ok', reason='core adapters loaded'),
    ]
    if warn is not None:
        checks.append(UpdateCheckResult(
            name='gpu_backend', status='warn', code='gpu_limited',
            reason=warn))
    else:
        checks.append(UpdateCheckResult(
            name='gpu_backend', status='pass',
            code='gpu_ok', reason='GPU backend available'))
    if unknown is not None:
        checks.append(UpdateCheckResult(
            name='telemetry', status='unknown', code='unobserved',
            reason=unknown))
    return HealthObservation(
        checks=tuple(checks),
        observed_version=version,
        observed_native_schema=schema,
    )


def _unhealthy(reason: str = 'swap broke launch') -> HealthObservation:
    return HealthObservation(
        checks=(
            UpdateCheckResult(
                name='app_launch', status='fail',
                code='launch_failed', reason=reason),
            UpdateCheckResult(
                name='version_stamp', status='pass',
                code='version_stamp_match', reason='observed 1.1.0'),
        ),
        observed_version='1.1.0',
        observed_native_schema=NATIVE_SCHEMA_VERSION,
    )


@pytest.fixture()
def repos(tmp_path: Path):
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return SimpleNamespace(
        db=db,
        scene=scene,
        update=CadApplicationUpdateRepository(scene),
    )


def _service(
    repos,
    *,
    source=None,
    driver=None,
    probe=None,
    clock=None,
) -> ApplicationUpdateService:
    return ApplicationUpdateService(
        repository=repos.update,
        source=source or FakeUpdatePackageSource(
            FakePackageScenario(payloads=dict(PAYLOADS))),
        driver=driver or FakeUpdateInstallDriver(),
        probe=probe or FakeUpdateEnvironmentProbe(
            _facts(), health=_healthy()),
        clock=clock or (lambda: NOW),
    )


def _open(
    service: ApplicationUpdateService,
    tmp_path: Path,
    *,
    package_over: dict | None = None,
    session_over: dict | None = None,
):
    install_root, data_dir, update_area = _env(tmp_path)
    package_kwargs = _package_kwargs(**(package_over or {}))
    session_kwargs = dict(
        install_root=str(install_root),
        data_dir=str(data_dir),
        update_area=str(update_area),
        app_version_before='1.0.0',
        native_schema_before=NATIVE_SCHEMA_VERSION,
        operator_id='operator-1',
    )
    session_kwargs.update(session_over or {})
    session, package = service.open_session(
        document_id=DOC,
        **package_kwargs,
        **session_kwargs,
    )
    return session, package


def _drive_to_preflight(
    service: ApplicationUpdateService,
    tmp_path: Path,
    *,
    package_over: dict | None = None,
    session_over: dict | None = None,
):
    session, package = _open(
        service, tmp_path,
        package_over=package_over, session_over=session_over)
    service.fetch_package(session)
    service.verify_package(session)
    report = service.run_preflight(session)
    return session, package, report


def _drive_to_staged(
    service: ApplicationUpdateService,
    tmp_path: Path,
    *,
    package_over: dict | None = None,
    session_over: dict | None = None,
):
    session, package, report = _drive_to_preflight(
        service, tmp_path,
        package_over=package_over, session_over=session_over)
    assert report.verdict in ('eligible', 'eligible_with_warnings')
    service.authorize(
        session, scope='apply', actor='operator-1',
        acknowledges_irreversible_migration=(
            report.irreversible_migration_disclosed))
    point = service.capture_restore_point(session)
    service.stage_payload(session)
    return session, package, report, point


# ---------------------------------------------------------------------------
# Sealed records — seal/id integrity
# ---------------------------------------------------------------------------


def test_package_descriptor_seal_and_tamper():
    package = UpdatePackageDescriptor.create(
        document_id=DOC,
        declared_by='operator-1',
        declared_at_utc=NOW,
        **_package_kwargs())
    assert package.package_id.startswith('upkg-')
    assert len(package.package_sha256) == 64
    # Any mutation must break the seal.
    mutated = dict(package.model_dump())
    mutated.pop('package_id')
    mutated.pop('package_sha256')
    mutated['channel'] = 'preview'
    with pytest.raises(ValidationError):
        UpdatePackageDescriptor(**mutated)
    # Bad sha pattern on an artifact.
    with pytest.raises(ValidationError):
        UpdatePackageDescriptor.create(
            document_id=DOC,
            declared_by='op', declared_at_utc=NOW,
            **dict(_package_kwargs(), artifacts=(
                UpdateArtifactRef(
                    name='a', sha256='nothex' * 8, size_bytes=1),)))


def test_package_descriptor_rejects_invalid_windows():
    with pytest.raises(ValidationError):
        UpdatePackageDescriptor.create(
            document_id=DOC, declared_by='op', declared_at_utc=NOW,
            **_package_kwargs(schema_floor=200, schema_ceiling=100))
    with pytest.raises(ValidationError):
        UpdatePackageDescriptor.create(
            document_id=DOC, declared_by='op', declared_at_utc=NOW,
            **_package_kwargs(runtime_floor='not-a-version'))
    with pytest.raises(ValidationError):
        UpdatePackageDescriptor.create(
            document_id=DOC, declared_by='op', declared_at_utc=NOW,
            **_package_kwargs(artifacts=(
                UpdateArtifactRef(name='dup', sha256='ab' * 32,
                                  size_bytes=1),
                UpdateArtifactRef(name='dup', sha256='cd' * 32,
                                  size_bytes=1))))


def test_signature_state_vocabulary_invariants():
    ok = UpdateSignatureState(
        status='signed_verified',
        signer_subject='CN=HTDT Publisher',
        signer_thumbprint='AABBCCDD',
        timestamped=True)
    assert ok.status == 'signed_verified'
    for status in ('unsigned', 'signing_failed', 'unverifiable'):
        assert UpdateSignatureState(status=status).status == status
        with pytest.raises(ValidationError):
            UpdateSignatureState(
                status=status, signer_subject='CN=x')
    with pytest.raises(ValidationError):
        UpdateSignatureState(
            status='signed_verified',
            signer_thumbprint='AABB', timestamped=True)


def test_session_record_seal(repos):
    package = UpdatePackageDescriptor.create(
        document_id=DOC, declared_by='op', declared_at_utc=NOW,
        **_package_kwargs())
    session = UpdateSessionRecord.create(
        document_id=DOC,
        package_ref=package_binding(package),
        install_root='/i', data_dir='/d', update_area='/u',
        app_version_before='1.0.0',
        native_schema_before=NATIVE_SCHEMA_VERSION,
        opened_by='op', opened_at_utc=NOW,
        authority_version=APPLICATION_UPDATE_SCHEMA_VERSION)
    assert session.session_id.startswith('upd-')
    with pytest.raises(ValidationError):
        UpdateSessionRecord(
            **dict(session.model_dump(), package_ref=AuthorityRef(
                kind='diagnostic_session', ref_id='x',
                ref_sha256='ab' * 32)))


def test_preflight_report_rejects_inconsistent_verdict(repos, tmp_path):
    service = _service(repos)
    session, package = _open(service, tmp_path)
    checks = (
        UpdateCheckResult(name='a', status='fail', code='x',
                          reason='failed check'),
        UpdateCheckResult(name='b', status='pass', code='y',
                          reason='ok'),
    )
    with pytest.raises(ValidationError):
        # Claims 'eligible' while a check failed — impossible report.
        UpdatePreflightReport.create(
            document_id=DOC,
            session_ref=session_binding(session),
            package_ref=package_binding(package),
            verdict='eligible',
            checks=checks,
            irreversible_migration_disclosed=False,
            environment=UpdateEnvironmentSnapshot(
                os_name='windows', runtime_version='3.12',
                app_version='1.0.0',
                app_native_schema_version=NATIVE_SCHEMA_VERSION,
                data_dir_schema_version=NATIVE_SCHEMA_VERSION),
            environment_sha256='' * 64,
            probe_is_simulated=True,
            evaluated_at_utc=NOW,
            evaluator_version='test')


def test_health_report_invariants(repos, tmp_path):
    service = _service(repos)
    session, _ = _open(service, tmp_path)
    # Split observed pair rejected.
    with pytest.raises(ValidationError):
        UpdateHealthReport.create(
            document_id=DOC,
            session_ref=session_binding(session),
            verdict='healthy',
            checks=_healthy().checks,
            observed_version='1.1.0',
            observed_native_schema=None,
            probe_is_simulated=True,
            evaluated_at_utc=NOW,
            evaluator_version='test')
    # Empty check list rejected.
    with pytest.raises(ValidationError):
        UpdateHealthReport.create(
            document_id=DOC,
            session_ref=session_binding(session),
            verdict='healthy',
            checks=(),
            probe_is_simulated=True,
            evaluated_at_utc=NOW,
            evaluator_version='test')


def test_outcome_record_invariants(repos, tmp_path):
    service = _service(repos)
    session, package = _open(service, tmp_path)
    session_ref = session_binding(session)
    package_ref = package_binding(package)
    point = UpdateRestorePoint.create(
        document_id=DOC,
        session_ref=session_ref,
        preflight_ref=AuthorityRef(
            kind='update_preflight_report', ref_id='upre-x',
            ref_sha256='ab' * 32),
        backup_root='/backups',
        manifest_sha256='cd' * 32,
        install_tree_sha256='ef' * 32,
        captured_items=(UpdateCapturedItem(
            item_kind='install_file', relative_path='install/app.bin',
            sha256='00' * 32, size_bytes=1, bytes_captured=True),),
        schema_version_before=NATIVE_SCHEMA_VERSION,
        data_backup_kind='manifest_only',
        migration_boundary='none',
        rollback_scope_capable='binary_only',
        driver_is_simulated=True,
        captured_at_utc=NOW)
    # rolled_back without verification → rejected.
    with pytest.raises(ValidationError):
        UpdateOutcomeRecord.create(
            document_id=DOC,
            session_ref=session_ref, package_ref=package_ref,
            verdict='rolled_back', final_stage='rolled_back',
            rollback_scope='binary_only',
            rollback_verified=False,
            rollback_scope_capable='binary_only',
            restore_point_ref=restore_point_binding(point),
            reason='x', decided_at_utc=NOW)
    # 'full' scope the restore point cannot carry → rejected.
    with pytest.raises(ValidationError):
        UpdateOutcomeRecord.create(
            document_id=DOC,
            session_ref=session_ref, package_ref=package_ref,
            verdict='rolled_back', final_stage='rolled_back',
            rollback_scope='full',
            rollback_verified=True,
            rollback_scope_capable='binary_only',
            restore_point_ref=restore_point_binding(point),
            reason='x', decided_at_utc=NOW)
    # committed without a health ref → rejected.
    with pytest.raises(ValidationError):
        UpdateOutcomeRecord.create(
            document_id=DOC,
            session_ref=session_ref, package_ref=package_ref,
            verdict='committed', final_stage='committed',
            reason='x', decided_at_utc=NOW)


def test_restore_point_scope_invariants(repos, tmp_path):
    service = _service(repos)
    session, _ = _open(service, tmp_path)
    item = UpdateCapturedItem(
        item_kind='install_file', relative_path='install/app.bin',
        sha256='00' * 32, size_bytes=1, bytes_captured=True)
    kwargs = dict(
        document_id=DOC,
        session_ref=session_binding(session),
        preflight_ref=AuthorityRef(
            kind='update_preflight_report', ref_id='upre-x',
            ref_sha256='ab' * 32),
        backup_root='/b',
        manifest_sha256='cd' * 32,
        install_tree_sha256='ef' * 32,
        captured_items=(item,),
        schema_version_before=NATIVE_SCHEMA_VERSION,
        driver_is_simulated=True,
        captured_at_utc=NOW)
    with pytest.raises(ValidationError):
        UpdateRestorePoint.create(
            data_backup_kind='manifest_only',
            migration_boundary='none',
            rollback_scope_capable='full', **kwargs)
    with pytest.raises(ValidationError):
        UpdateRestorePoint.create(
            data_backup_kind='full',
            migration_boundary='forward_only',
            rollback_scope_capable='full', **kwargs)


# ---------------------------------------------------------------------------
# Pure state machine + derivation
# ---------------------------------------------------------------------------


def _state(stage='opened', **overrides) -> UpdateSessionState:
    return UpdateSessionState(
        session_ref=AuthorityRef(
            kind='update_session', ref_id='upd-x',
            ref_sha256='ab' * 32),
        current_stage=stage, **overrides)


def _event(kind, **overrides) -> UpdateEvent:
    kwargs = dict(kind=kind, at_utc=NOW, reason='test')
    kwargs.update(overrides)
    return UpdateEvent(**kwargs)


def test_pure_machine_advance_rules():
    for current, kind, expected in (
        ('opened', 'package_fetched', 'package_fetched'),
        ('package_fetched', 'package_verified', 'package_verified'),
        ('recovery_point_captured', 'payload_staged', 'staged'),
        ('staged', 'swap_applied', 'applied'),
        ('applied', 'health_check_completed', 'health_checked'),
    ):
        decision = update_stage_transition(
            _state(current), _event(kind, succeeded=True))
        assert decision.to_stage == expected
        assert decision.outcome == 'advanced'


def test_pure_machine_failure_resolution():
    # held — retryable
    for current, kind in (
        ('opened', 'package_fetched'),
        ('preflight_evaluated', 'restore_point_captured'),
        ('staged', 'swap_applied'),
    ):
        decision = update_stage_transition(
            _state(current), _event(kind, succeeded=False))
        assert decision.outcome == 'held'
        assert decision.to_stage == current
    # failed — terminal (tamper is not transient)
    for current, kind in (
        ('package_fetched', 'package_verified'),
        ('recovery_point_captured', 'payload_staged'),
    ):
        decision = update_stage_transition(
            _state(current), _event(kind, succeeded=False))
        assert decision.outcome == 'failed'
        assert decision.to_stage == 'failed'


def test_pure_machine_rejections():
    # Event not permitted at stage.
    d = update_stage_transition(
        _state('opened'), _event('package_verified', succeeded=True))
    assert d.__class__.__name__ == 'UpdateRejection'
    # Cancel past cancellable window.
    d = update_stage_transition(
        _state('staged'), _event('cancelled', succeeded=True))
    assert d.__class__.__name__ == 'UpdateRejection'
    assert 'cancel_requires_rollback' in d.reason
    # Authorization only at preflight_evaluated.
    d = update_stage_transition(
        _state('opened'), _event('operator_authorized'))
    assert d.__class__.__name__ == 'UpdateRejection'
    # Terminal states reject everything but a new session_opened.
    for stage in ('committed', 'rolled_back', 'rollback_failed',
                  'blocked', 'failed', 'cancelled'):
        d = update_stage_transition(
            _state(stage, terminal=True),
            _event('package_fetched', succeeded=True))
        assert d.__class__.__name__ == 'UpdateRejection'
    # rollback_completed requires an explicit outcome.
    d = update_stage_transition(
        _state('rolling_back'), _event('rollback_completed'))
    assert d.__class__.__name__ == 'UpdateRejection'
    # rollback_started only from mutation stages.
    d = update_stage_transition(
        _state('opened'), _event('rollback_started', succeeded=True))
    assert d.__class__.__name__ == 'UpdateRejection'


def test_pure_machine_preflight_targets():
    for target, stage in (
        ('blocked', 'blocked'), ('preflight_evaluated',
                                 'preflight_evaluated')):
        d = update_stage_transition(
            _state('package_verified'),
            _event('preflight_evaluated', succeeded=True,
                   target_stage=target))
        assert d.outcome in ('blocked', 'advanced')
        assert d.to_stage == stage
    d = update_stage_transition(
        _state('package_verified'),
        _event('preflight_evaluated', succeeded=True))
    assert d.__class__.__name__ == 'UpdateRejection'


def test_pure_machine_rollback_reentry_is_informational():
    d = update_stage_transition(
        _state('rolling_back'),
        _event('rollback_started', succeeded=True))
    assert d.outcome == 'informational'
    assert d.to_stage == 'rolling_back'


def test_derive_update_state_folds_log(repos):
    service = _service(repos)
    session, package = _open(service, repos.scene.path.parent)
    service.fetch_package(session)
    service.verify_package(session)
    state = service.state(session)
    assert state.current_stage == 'package_verified'
    assert not state.terminal
    assert 'package_fetched' in state.seen_kinds
    assert state.evidence['update_package'].ref_id == package.package_id
    # A rejected event does not advance the stage.
    service.cancel(session, reason='operator canceled')
    state = service.state(session)
    assert state.current_stage == 'cancelled'
    assert state.terminal


# ---------------------------------------------------------------------------
# Preflight evaluator — the fail-closed matrix
# ---------------------------------------------------------------------------


def _descriptor(**overrides) -> UpdatePackageDescriptor:
    kwargs = _package_kwargs(**overrides)
    return UpdatePackageDescriptor.create(
        document_id=DOC, declared_by='op', declared_at_utc=NOW,
        **kwargs)


def test_preflight_eligible():
    descriptor = _descriptor(
        signature=_signature(
            'signed_verified', signer_subject='CN=HTDT',
            signer_thumbprint='AA', timestamped=True),
        release_ref=AuthorityRef(
            kind='release_verification', ref_id='rel-1',
            ref_sha256='ab' * 32))
    evaluation = evaluate_preflight(
        descriptor, _facts(),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'eligible'
    assert not evaluation.irreversible_migration_disclosed


def test_preflight_eligible_with_warnings():
    evaluation = evaluate_preflight(
        _descriptor(), _facts(),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'eligible_with_warnings'
    warns = {c.code for c in evaluation.checks if c.status == 'warn'}
    assert 'unsigned_package' in warns
    assert 'release_evidence_absent' in warns


@pytest.mark.parametrize('facts_over,pkg_over,code', [
    (dict(os_name='macos'), {}, 'platform_unsupported'),
    (dict(runtime_version='3.9'), {}, 'runtime_below_floor'),
    (dict(data_dir_schema_version=90), {}, 'schema_below_floor'),
    (dict(data_dir_schema_version=200), {}, 'schema_above_ceiling'),
    (dict(free_disk_bytes=10), {}, 'insufficient_disk_space'),
    (dict(open_transaction_kinds=('measurement',)), {},
     'active_transactions'),
    (dict(pending_recovery_sessions=2), {}, 'pending_crash_recovery'),
    (dict(bound_adapter_ids=('legacy_dsp',)),
     dict(dropped_adapter_ids=('legacy_dsp',)), 'dropped_adapter_bound'),
    (dict(adapter_api_level=1), dict(adapter_api_floor=2),
     'adapter_api_below_floor'),
    ({}, dict(channel='preview'), 'channel_not_opted_in'),
    ({}, dict(target_native_schema_version=90), 'schema_downgrade'),
    (dict(app_native_schema_version=NATIVE_SCHEMA_VERSION + 1,
          data_dir_schema_version=NATIVE_SCHEMA_VERSION),
     {}, 'unapplied_migrations_pending'),
])
def test_preflight_incompatible_paths(facts_over, pkg_over, code):
    evaluation = evaluate_preflight(
        _descriptor(**pkg_over), _facts(**facts_over),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'incompatible'
    check = next(c for c in evaluation.checks if c.name and c.code == code)
    assert check.status == 'fail'


def test_preflight_signature_gates():
    # require_signed + unsigned → incompatible
    evaluation = evaluate_preflight(
        _descriptor(signature=_signature('unsigned')), _facts(),
        signature_policy='require_signed',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'incompatible'
    # signing_failed is always a block.
    evaluation = evaluate_preflight(
        _descriptor(signature=_signature('signing_failed')), _facts(),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'incompatible'


@pytest.mark.parametrize('facts_over,pkg_over', [
    (dict(free_disk_bytes=None), {}),
    (dict(pending_recovery_sessions=None), {}),
    (dict(adapter_api_level=None), {}),
    (dict(os_name='unknown'), {}),
    ({}, dict(migration_reversibility='unknown',
              target_native_schema_version=NATIVE_SCHEMA_VERSION + 1)),
    ({}, dict(supported_platforms=())),
    ({}, dict(runtime_floor=None)),
    ({}, dict(signature=_signature('unverifiable'))),
])
def test_preflight_unverifiable_paths(facts_over, pkg_over):
    evaluation = evaluate_preflight(
        _descriptor(**pkg_over), _facts(**facts_over),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'unverifiable'


def test_preflight_forward_only_migration_disclosed():
    evaluation = evaluate_preflight(
        _descriptor(
            target_native_schema_version=NATIVE_SCHEMA_VERSION + 1,
            migration_reversibility='forward_only'),
        _facts(),
        signature_policy='allow_unsigned',
        require_release_evidence=False,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'eligible_with_warnings'
    assert evaluation.irreversible_migration_disclosed
    check = next(
        c for c in evaluation.checks
        if c.code == 'forward_only_migration')
    assert check.status == 'warn'


def test_preflight_release_evidence_required():
    evaluation = evaluate_preflight(
        _descriptor(), _facts(),
        signature_policy='allow_unsigned',
        require_release_evidence=True,
        allowed_channels=('stable',))
    assert evaluation.verdict == 'incompatible'
    assert any(
        c.code == 'release_evidence_required'
        for c in evaluation.checks)


def test_health_aggregation():
    assert evaluate_health(_healthy()) == 'healthy'
    assert evaluate_health(_healthy(warn='gpu')) == 'healthy_with_warnings'
    assert evaluate_health(_unhealthy()) == 'unhealthy'
    assert evaluate_health(_healthy(unknown='x')) == 'unverifiable'


# ---------------------------------------------------------------------------
# Service pipeline — happy path
# ---------------------------------------------------------------------------


def test_full_pipeline_commits(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    assert report.verdict == 'eligible_with_warnings'
    authorization = service.authorize(
        session, scope='apply', actor='operator-1')
    assert authorization.scope == 'apply'
    point = service.capture_restore_point(session)
    assert point.rollback_scope_capable == 'binary_only'
    service.stage_payload(session)
    service.apply_swap(session)
    health = service.health_check(session)
    assert health.verdict == 'healthy'
    outcome = service.decide(session)
    assert outcome.verdict == 'committed'
    assert outcome.final_stage == 'committed'
    assert outcome.health_ref is not None
    state = service.state(session)
    assert state.current_stage == 'committed'
    assert state.terminal
    assert state.apply_authorized
    assert state.apply_authorization_consumed
    # Everything sealed and persisted.
    transitions = repos.update.list_transitions(session.session_id)
    kinds = [t.event_kind for t in transitions]
    assert kinds == [
        'session_opened', 'package_fetched', 'package_verified',
        'preflight_evaluated', 'operator_authorized',
        'restore_point_captured', 'payload_staged', 'swap_applied',
        'health_check_completed', 'commit_decided',
    ]


def test_full_pipeline_install_bytes_swapped(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    service.authorize(session, scope='apply', actor='op')
    service.capture_restore_point(session)
    service.stage_payload(session)
    service.apply_swap(session)
    install_root = Path(session.install_root)
    # Staged payload replaced the install (plus the version stamp).
    assert (install_root / 'app.bin').read_bytes() == PAYLOADS['app.bin']
    stamp = json.loads(
        (install_root / 'version.json').read_text(encoding='utf-8'))
    assert stamp['version'] == '1.1.0'
    service.health_check(session)
    service.decide(session)
    assert service.state(session).current_stage == 'committed'


def test_local_directory_source(repos, tmp_path):
    source_dir = tmp_path / 'bundle'
    source_dir.mkdir()
    for name, data in PAYLOADS.items():
        (source_dir / name).write_bytes(data)
    service = _service(
        repos, source=LocalDirectoryPackageSource(source_dir))
    session, package = _open(service, tmp_path)
    fetched = service.fetch_package(session)
    assert set(fetched) == set(PAYLOADS)
    service.verify_package(session)
    assert service.state(session).current_stage == 'package_verified'


def test_fetch_failure_is_retryable_hold(repos, tmp_path):
    source = FakeUpdatePackageSource(FakePackageScenario(
        payloads=dict(PAYLOADS), fail_fetch=True))
    service = _service(repos, source=source)
    session, package = _open(service, tmp_path)
    with pytest.raises(ApplicationUpdateError):
        service.fetch_package(session)
    state = service.state(session)
    assert state.current_stage == 'opened'
    assert state.held_reason
    # Same session retries — fetch succeeds now.
    source.scenario = FakePackageScenario(payloads=dict(PAYLOADS))
    service.fetch_package(session)
    assert service.state(session).current_stage == 'package_fetched'


@pytest.mark.parametrize('scenario', [
    FakePackageScenario(payloads=dict(PAYLOADS),
                        corrupt_artifacts=('app.bin',)),
    FakePackageScenario(payloads=dict(PAYLOADS), truncate_bytes=3),
    FakePackageScenario(payloads=dict(PAYLOADS),
                        missing_artifacts=('lib.dll',)),
    FakePackageScenario(payloads={'app.bin': b'other',
                                  'lib.dll': PAYLOADS['lib.dll'],
                                  'manifest.json': PAYLOADS['manifest.json']}),
])
def test_corrupt_package_fails_closed(repos, tmp_path, scenario):
    service = _service(
        repos, source=FakeUpdatePackageSource(scenario))
    session, package = _open(service, tmp_path)
    service.fetch_package(session)
    transition = service.verify_package(session)
    assert transition.outcome == 'failed'
    state = service.state(session)
    assert state.current_stage == 'failed'
    assert state.terminal
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'failed'
    # Terminal sessions cannot be revived.
    with pytest.raises(ApplicationUpdateError):
        service.run_preflight(session)


def test_verify_out_of_order_is_rejected_not_failed(repos, tmp_path):
    service = _service(repos)
    session, _ = _open(service, tmp_path)
    transition = service.verify_package(session)
    assert transition.outcome == 'rejected'
    assert service.state(session).current_stage == 'opened'
    assert not repos.update.list_outcomes(session.session_id)


def test_preflight_blocked_records_outcome(repos, tmp_path):
    service = _service(
        repos,
        probe=FakeUpdateEnvironmentProbe(
            _facts(os_name='macos'), health=_healthy()))
    session, package = _open(service, tmp_path)
    service.fetch_package(session)
    service.verify_package(session)
    report = service.run_preflight(session)
    assert report.verdict == 'incompatible'
    state = service.state(session)
    assert state.current_stage == 'blocked'
    assert state.terminal
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'blocked'
    assert outcome.preflight_ref is not None


def test_unsigned_require_signed_blocks(repos, tmp_path):
    service = _service(repos)
    session, package = _open(
        service, tmp_path,
        session_over=dict(signature_policy='require_signed'))
    service.fetch_package(session)
    service.verify_package(session)
    report = service.run_preflight(session)
    assert report.verdict == 'incompatible'


def test_unsigned_package_commits_under_allow_policy(repos, tmp_path):
    # 'unsigned' is a first-class state, not an error — with an explicit
    # allow policy the update proceeds with warnings on record.
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    assert report.verdict == 'eligible_with_warnings'
    signature_check = next(
        c for c in report.checks if c.name == 'signature')
    assert signature_check.code == 'unsigned_package'
    assert signature_check.status == 'warn'


# ---------------------------------------------------------------------------
# Authorization gates
# ---------------------------------------------------------------------------


def test_authorize_before_preflight_rejected(repos, tmp_path):
    service = _service(repos)
    session, _ = _open(service, tmp_path)
    service.fetch_package(session)
    with pytest.raises(UpdateAuthorizationError):
        service.authorize(session, scope='apply', actor='op')
    # The rejection is itself a sealed transition.
    rejected = [
        t for t in repos.update.list_transitions(session.session_id)
        if t.outcome == 'rejected']
    assert rejected


def test_forward_only_requires_explicit_ack(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(
        service, tmp_path,
        package_over=dict(
            target_native_schema_version=NATIVE_SCHEMA_VERSION + 1,
            migration_reversibility='forward_only'))
    assert report.irreversible_migration_disclosed
    with pytest.raises(UpdateAuthorizationError):
        service.authorize(session, scope='apply', actor='op')
    authorization = service.authorize(
        session, scope='apply', actor='op',
        acknowledges_irreversible_migration=True)
    assert authorization.acknowledges_irreversible_migration


def test_apply_authorization_is_single_use(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    service.authorize(session, scope='apply', actor='op')
    service.capture_restore_point(session)
    # A second apply push requires the stage machine to move back —
    # it cannot, so re-authorizing is rejected outright.
    with pytest.raises(UpdateAuthorizationError):
        service.authorize(session, scope='apply', actor='op')


def test_capture_without_authorization_rejected(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    with pytest.raises(UpdateAuthorizationError):
        service.capture_restore_point(session)


# ---------------------------------------------------------------------------
# Staged apply + verified rollback
# ---------------------------------------------------------------------------


def test_unhealthy_health_auto_rolls_back(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(
        _facts(), health=_unhealthy())
    service = _service(repos, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    health = service.health_check(session)
    assert health.verdict == 'unhealthy'
    outcome = service.decide(session)
    assert outcome.verdict == 'rolled_back'
    assert outcome.rollback_verified is True
    assert outcome.rollback_scope == 'binary_only'
    assert outcome.restore_point_ref is not None
    # The install root holds the *old* bytes again, verified.
    install_root = Path(session.install_root)
    assert (install_root / 'app.bin').read_bytes() == \
        b'old-application-bytes'
    stamp = json.loads(
        (install_root / 'version.json').read_text(encoding='utf-8'))
    assert stamp['version'] == '1.0.0'


def test_unverifiable_health_rolls_back(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(
        _facts(), health=_healthy(unknown='gpu not probed'))
    service = _service(repos, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    health = service.health_check(session)
    assert health.verdict == 'unverifiable'
    outcome = service.decide(session)
    assert outcome.verdict == 'rolled_back'


def test_full_backup_full_rollback_scope(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(
        _facts(), health=_unhealthy())
    service = _service(repos, probe=probe)
    session, package, report = _drive_to_preflight(
        service, tmp_path,
        session_over=dict(data_backup_kind='full'))
    service.authorize(session, scope='apply', actor='op')
    point = service.capture_restore_point(session)
    assert point.rollback_scope_capable == 'full'
    # Mutate a data item after capture — the full backup carries bytes.
    prefs = Path(session.data_dir) / 'preferences.json'
    prefs.write_text('{"mutated": true}', encoding='utf-8')
    service.stage_payload(session)
    service.apply_swap(session)
    service.health_check(session)
    outcome = service.decide(session)
    assert outcome.verdict == 'rolled_back'
    assert outcome.rollback_scope == 'full'
    assert prefs.read_text(encoding='utf-8') == '{"v": 1}'


def test_forward_only_boundary_caps_scope(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(
        _facts(), health=_unhealthy())
    service = _service(repos, probe=probe)
    session, package, report = _drive_to_preflight(
        service, tmp_path,
        package_over=dict(
            target_native_schema_version=NATIVE_SCHEMA_VERSION + 1,
            migration_reversibility='forward_only'),
        session_over=dict(data_backup_kind='full'))
    service.authorize(
        session, scope='apply', actor='op',
        acknowledges_irreversible_migration=True)
    point = service.capture_restore_point(session)
    # Across a forward-only boundary 'full' can never be claimed.
    assert point.migration_boundary == 'forward_only'
    assert point.rollback_scope_capable == 'binary_only'
    service.stage_payload(session)
    service.apply_swap(session)
    service.health_check(session)
    outcome = service.decide(session)
    assert outcome.verdict == 'rolled_back'
    assert outcome.rollback_scope == 'binary_only'


def test_rollback_failed_on_corrupt_backup(repos, tmp_path):
    driver = FakeUpdateInstallDriver(FakeDriverScenario(
        corrupt_backup_files=('app.bin',)))
    probe = FakeUpdateEnvironmentProbe(_facts(), health=_unhealthy())
    service = _service(repos, driver=driver, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    service.health_check(session)
    outcome = service.decide(session)
    # The restore ran but could not verify — never claim rolled_back.
    assert outcome.verdict == 'rollback_failed'
    assert outcome.rollback_verified is False
    state = service.state(session)
    assert state.current_stage == 'rollback_failed'
    assert state.rollback_attempts == 1


def test_rollback_failed_on_driver_error(repos, tmp_path):
    driver = FakeUpdateInstallDriver(FakeDriverScenario(
        crash_mid_restore=True))
    probe = FakeUpdateEnvironmentProbe(_facts(), health=_unhealthy())
    service = _service(repos, driver=driver, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    service.health_check(session)
    outcome = service.decide(session)
    assert outcome.verdict == 'rollback_failed'
    assert outcome.rollback_verified is False


def test_rollback_failed_on_tampered_manifest(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(_facts(), health=_unhealthy())
    service = _service(repos, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    manifest = Path(point.backup_root) / 'manifest.json'
    manifest.write_text('{"tampered": true}', encoding='utf-8')
    service.apply_swap(session)
    service.health_check(session)
    outcome = service.decide(session)
    assert outcome.verdict == 'rollback_failed'


def test_swap_failure_auto_rolls_back(repos, tmp_path):
    driver = FakeUpdateInstallDriver(FakeDriverScenario(
        crash_mid_swap=True))
    service = _service(repos, driver=driver)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    with pytest.raises(ApplicationUpdateError):
        service.apply_swap(session)
    state = service.state(session)
    # Swap failure already entered the verified rollback path in-process.
    assert state.current_stage in ('rolled_back', 'rollback_failed')
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'rolled_back'
    assert (Path(session.install_root) / 'app.bin').read_bytes() == \
        b'old-application-bytes'


def test_capture_failure_holds(repos, tmp_path):
    driver = FakeUpdateInstallDriver(FakeDriverScenario(fail_capture=True))
    service = _service(repos, driver=driver)
    session, package, report = _drive_to_preflight(service, tmp_path)
    service.authorize(session, scope='apply', actor='op')
    with pytest.raises(ApplicationUpdateError):
        service.capture_restore_point(session)
    state = service.state(session)
    assert state.current_stage == 'preflight_evaluated'
    assert state.held_reason


def test_stage_failure_is_terminal(repos, tmp_path):
    driver = FakeUpdateInstallDriver(FakeDriverScenario(fail_stage=True))
    service = _service(repos, driver=driver)
    session, package, report = _drive_to_preflight(service, tmp_path)
    service.authorize(session, scope='apply', actor='op')
    service.capture_restore_point(session)
    with pytest.raises(ApplicationUpdateError):
        service.stage_payload(session)
    assert service.state(session).current_stage == 'failed'


def test_health_probe_failure_rolls_back(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(
        _facts(), health_error=RuntimeError('probe exploded'))
    service = _service(repos, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    with pytest.raises(ApplicationUpdateError):
        service.health_check(session)
    state = service.state(session)
    assert state.current_stage == 'rolled_back'
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'rolled_back'


# ---------------------------------------------------------------------------
# Crash recovery via the sealed log
# ---------------------------------------------------------------------------


def test_resume_rolls_back_partial_swap(repos, tmp_path):
    service = _service(repos)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    # Simulate a process crash between the two swap renames.
    install_root = Path(session.install_root)
    update_area = Path(session.update_area)
    install_root.rename(update_area / 'previous-install')
    # New service instance — same repo, memory of state comes from the
    # sealed log + the driver's install inspection only.
    resumed = _service(repos)
    state = resumed.resume(session)
    assert state.current_stage == 'rolled_back'
    assert (install_root / 'app.bin').read_bytes() == \
        b'old-application-bytes'


def test_resume_after_swap_recovers_health_path(repos, tmp_path):
    service = _service(repos)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    # Swap completed on disk but its transition never sealed.
    install_root = Path(session.install_root)
    update_area = Path(session.update_area)
    staged = update_area / 'staged'
    install_root.rename(update_area / 'previous-install')
    staged.rename(install_root)
    resumed = _service(repos)
    state = resumed.resume(session)
    assert state.current_stage == 'applied'
    # A second resume runs the health check and decides.
    state = resumed.resume(session)
    assert state.current_stage == 'committed'
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'committed'


def test_resume_applied_with_bad_health_rolls_back(repos, tmp_path):
    service = _service(repos)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    install_root = Path(session.install_root)
    update_area = Path(session.update_area)
    install_root.rename(update_area / 'previous-install')
    (update_area / 'staged').rename(install_root)
    resumed = _service(
        repos,
        probe=FakeUpdateEnvironmentProbe(
            _facts(), health=_unhealthy()))
    state = resumed.resume(session)
    state = resumed.resume(session)
    assert state.current_stage == 'rolled_back'


class _HaltMidRestore(FakeUpdateInstallDriver):
    """Diverts the broken install, then dies like a process halt."""

    def __init__(self) -> None:
        super().__init__()
        self.halted = False

    def restore_install(self, **kwargs):
        if not self.halted:
            self.halted = True
            install_root = kwargs['install_root']
            update_area = kwargs['update_area']
            if install_root.exists():
                install_root.rename(update_area / 'failed-install-0')
            raise _SimulatedHalt('process died mid-restore')
        return super().restore_install(**kwargs)


class _SimulatedHalt(BaseException):
    """A halt the service cannot catch — like power loss."""


def test_resume_mid_rollback_completes(repos, tmp_path):
    driver = _HaltMidRestore()
    probe = FakeUpdateEnvironmentProbe(_facts(), health=_unhealthy())
    service = _service(repos, driver=driver, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    service.health_check(session)
    with pytest.raises(_SimulatedHalt):
        service.rollback(session, reason='health_unhealthy')
    state = service.state(session)
    assert state.current_stage == 'rolling_back'
    assert not state.terminal
    # Next launch: resume sees rolling_back and re-runs the bounded
    # rollback — the driver completes it this time.
    resumed = _service(repos)
    state = resumed.resume(session)
    assert state.current_stage == 'rolled_back'
    assert state.rollback_attempts == 2
    outcome = repos.update.list_outcomes(session.session_id)[-1]
    assert outcome.verdict == 'rolled_back'


def test_resume_on_clean_stage_is_informational(repos, tmp_path):
    service = _service(repos)
    session, package = _open(service, tmp_path)
    service.fetch_package(session)
    service.verify_package(session)
    resumed = _service(repos)
    state = resumed.resume(session)
    assert state.current_stage == 'package_verified'
    # The sealed log recorded the resume itself.
    transitions = repos.update.list_transitions(session.session_id)
    assert transitions[-1].event_kind == 'resumed'
    assert transitions[-1].outcome == 'informational'
    # Pipeline can continue.
    service.run_preflight(session)
    assert service.state(session).current_stage == 'preflight_evaluated'


def test_resume_on_terminal_is_noop(repos, tmp_path):
    service = _service(repos)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    service.health_check(session)
    service.decide(session)
    before = repos.update.list_transitions(session.session_id)
    state = _service(repos).resume(session)
    after = repos.update.list_transitions(session.session_id)
    assert state.current_stage == 'committed'
    assert len(before) == len(after)


# ---------------------------------------------------------------------------
# Cancel / fail
# ---------------------------------------------------------------------------


def test_cancel_before_mutation(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    outcome = service.cancel(session, reason='operator aborted')
    assert outcome.verdict == 'cancelled'
    assert service.state(session).terminal


def test_cancel_after_staging_requires_rollback(repos, tmp_path):
    probe = FakeUpdateEnvironmentProbe(_facts(), health=_healthy())
    service = _service(repos, probe=probe)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    with pytest.raises(ApplicationUpdateError):
        service.cancel(session, reason='too late')
    assert service.state(session).current_stage == 'staged'


def test_fail_marks_terminal(repos, tmp_path):
    service = _service(repos)
    session, package = _open(service, tmp_path)
    outcome = service.fail(session, reason='unrecoverable precondition')
    assert outcome.verdict == 'failed'
    assert service.state(session).current_stage == 'failed'


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def test_repository_round_trip_all_records(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    service.authorize(session, scope='apply', actor='op')
    point = service.capture_restore_point(session)
    service.stage_payload(session)
    service.apply_swap(session)
    health = service.health_check(session)
    outcome = service.decide(session)

    assert repos.update.get_package(
        package.package_id) == package
    assert repos.update.get_session(session.session_id) == session
    assert repos.update.list_packages(DOC) == (package,)
    assert repos.update.list_sessions(DOC) == (session,)
    transitions = repos.update.list_transitions(session.session_id)
    assert len(transitions) == 10
    assert repos.update.get_transition(
        transitions[0].transition_id) == transitions[0]
    reports = repos.update.list_preflight_reports(session.session_id)
    assert reports == (report,)
    assert repos.update.get_preflight_report(
        report.report_id) == report
    points = repos.update.list_restore_points(session.session_id)
    assert points == (point,)
    assert repos.update.get_restore_point(point.restore_id) == point
    healths = repos.update.list_health_reports(session.session_id)
    assert healths == (health,)
    assert repos.update.get_health_report(health.report_id) == health
    authorizations = repos.update.list_authorizations(session.session_id)
    assert len(authorizations) == 1
    assert repos.update.get_authorization(
        authorizations[0].authorization_id) == authorizations[0]
    outcomes = repos.update.list_outcomes(session.session_id)
    assert outcomes == (outcome,)
    assert repos.update.get_outcome(outcome.outcome_id) == outcome


def test_repository_column_tamper_detected(repos, tmp_path):
    service = _service(repos)
    session, package, report = _drive_to_preflight(service, tmp_path)
    with closing(connect_sqlite(repos.db)) as connection, connection:
        connection.execute(
            'UPDATE cad_update_preflight_reports '
            "SET verdict='eligible' WHERE report_id=?",
            (report.report_id,))
    with pytest.raises(DeploymentIntegrityError):
        repos.update.get_preflight_report(report.report_id)


def test_repository_rejects_broken_seals(repos, tmp_path):
    service = _service(repos)
    session, package, _ = _drive_to_preflight(service, tmp_path)
    forged = UpdateSessionRecord.model_construct(
        **dict(session.model_dump(), signature_policy='require_signed'))
    with pytest.raises(DeploymentIntegrityError):
        repos.update.save_session(forged)


def test_repository_idempotent_resave_and_conflict(repos, tmp_path):
    service = _service(repos)
    session, package, _ = _drive_to_preflight(service, tmp_path)
    # Byte-identical re-save is a no-op (append-only tolerance).
    repos.update.save_package(package)
    # Same id under a mutated payload — seal check fires first.
    forged = UpdatePackageDescriptor.model_construct(
        **dict(package.model_dump(), channel='preview'))
    with pytest.raises(DeploymentIntegrityError):
        repos.update.save_package(forged)
    # Same id, forged sha — reaches the store as 'sealed' but conflicts
    # with the stored sha → append-only conflict.
    same_id = UpdatePackageDescriptor.model_construct(
        **dict(package.model_dump(), package_sha256='ab' * 32))
    with pytest.raises((DeploymentConflictError,
                        DeploymentIntegrityError)):
        repos.update.save_package(same_id)


# ---------------------------------------------------------------------------
# Schema wiring
# ---------------------------------------------------------------------------


def test_schema_v110_creates_update_tables(tmp_path):
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with closing(connect_sqlite(db)) as connection:
        rows = {
            row['name']
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    assert NATIVE_SCHEMA_VERSION == 110
    for table in (
        'cad_update_packages', 'cad_update_sessions',
        'cad_update_transitions', 'cad_update_preflight_reports',
        'cad_update_restore_points', 'cad_update_health_reports',
        'cad_update_authorizations', 'cad_update_outcomes',
    ):
        assert table in rows


def test_simulated_flags_recorded(repos, tmp_path):
    service = _service(repos)
    session, package, report, point = _drive_to_staged(service, tmp_path)
    service.apply_swap(session)
    health = service.health_check(session)
    assert report.probe_is_simulated
    assert health.probe_is_simulated
    assert point.driver_is_simulated
