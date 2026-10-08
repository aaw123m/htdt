"""#903 deterministic fault-injection recovery drill harness.

Runs the *named fault x recovery expectation* matrix over the landed
recovery authorities and reports honestly per drill:

- #883 session recovery — crash mid-write / torn-tail journals,
  dirty-state restore, reconciliation honesty.
- #889 update sessions — mid-swap kill, mid-rollback kill,
  unverifiable post-swap health (must never commit).
- #884 diagnostic bundle — export under collector failure; errors
  recorded, never claimed clean.
- #890 credential vault — locked / unavailable paths deny material
  without dangling references or silent loss.
- #815 error boundaries — authority failures classify AUTHORITY and
  surface as errors, never fabricated success.

Verdict vocabulary (fixed by the slice contract):

- ``recovered_as_designed`` — the authority behaved as designed;
- ``loss_detected`` — expected state/records/data missing or corrupted;
- ``false_success`` — a success claim where evidence forbids one;
- ``harness_error`` — the drill itself failed before it could judge.

The matrix is data: ``builtin_drill_manifest()`` is mirrored verbatim
into ``scripts/recovery_drill_manifest.json`` so #903 acceptance can
enumerate it without importing the package. The harness adds no sealed
authority table — drills exercise *existing* authorities inside their
own sandbox data dir and the CLI seals one ``CadHeadlessRunRecord``
per ``drill run`` invocation (see issue doc for the decision).
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256

RECOVERY_DRILL_MANIFEST_FORMAT = 'htdt-recovery-drill-manifest-1'
RECOVERY_DRILL_REPORT_FORMAT = 'htdt-recovery-drill-report-1'

RecoveryDrillVerdict = Literal[
    'recovered_as_designed',
    'loss_detected',
    'false_success',
    'harness_error',
]

#: Verdicts that fail the drill — a passing matrix contains only
#: ``recovered_as_designed`` results.
FAILING_DRILL_VERDICTS: frozenset[str] = frozenset(
    {'loss_detected', 'false_success', 'harness_error'})

#: The named faults the harness knows how to inject. Each maps to one
#: concrete interruption point in an authority under test.
RecoveryDrillFault = Literal[
    'process_kill_mid_write',
    'journal_tail_truncation',
    'mid_file_corruption',
    'kill_mid_swap',
    'kill_mid_rollback',
    'health_probe_unverifiable',
    'context_collector_failure',
    'vault_locked',
    'vault_unavailable',
    'sealed_record_tamper',
]

#: Assertion kinds a check may carry. ``honesty`` failures mean the
#: authority reported something the evidence does not support
#: (-> ``false_success``); ``preservation``/``expectation`` failures
#: mean something recoverable was lost or the designed path was not
#: taken (-> ``loss_detected``).
DrillCheckKind = Literal['expectation', 'honesty', 'preservation']


class RecoveryDrillExpectation(BaseModel):
    """Structured expectation carried by each manifest entry."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    #: Terminal state token the drill must observe.
    expected_terminal: str = Field(min_length=1)
    #: Sealed-record / artifact kinds that must exist afterwards.
    expected_records: tuple[str, ...] = ()
    #: Observed terminals that would constitute a false-success claim.
    forbidden_terminals: tuple[str, ...] = ()
    #: Check ids the implementation must emit (absent -> harness_error).
    required_checks: tuple[str, ...] = ()


class RecoveryDrillSpec(BaseModel):
    """One manifest entry: a named fault x a recovery expectation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    drill_id: str = Field(min_length=1, pattern=r'^[a-z0-9][a-z0-9-]*$')
    title: str = Field(min_length=1)
    issue_refs: tuple[str, ...] = Field(min_length=1)
    fault: RecoveryDrillFault
    fault_summary: str = Field(min_length=1)
    recovery_expectation: str = Field(min_length=1)
    expectation: RecoveryDrillExpectation
    #: Which implementation runs the fault (see ``_IMPLEMENTATIONS``).
    implementation: str = Field(min_length=1)
    #: Mutating drills write sealed evidence into their sandbox and
    #: require the CLI's ``--arm`` flag.
    mutating: bool = True


class RecoveryDrillManifest(BaseModel):
    """The enumerable drill matrix — the data contract for #903."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    format: Literal['htdt-recovery-drill-manifest-1'] = (
        RECOVERY_DRILL_MANIFEST_FORMAT)
    manifest_version: int = Field(default=1, ge=1)
    drills: tuple[RecoveryDrillSpec, ...]
    manifest_sha256: str = Field(default='')

    @model_validator(mode='after')
    def _seal(self) -> 'RecoveryDrillManifest':
        payload = self.model_dump(mode='json')
        payload['manifest_sha256'] = ''
        expected = canonical_sha256(payload)
        if not self.manifest_sha256:
            object.__setattr__(self, 'manifest_sha256', expected)
        elif self.manifest_sha256 != expected:
            raise ValueError(
                'recovery drill manifest_sha256 does not match content')
        return self


class DrillCheck(BaseModel):
    """One evaluated assertion inside a drill observation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    check_id: str = Field(min_length=1, pattern=r'^[a-z0-9_.-]+$')
    kind: DrillCheckKind
    passed: bool
    detail: str = ''


class DrillObservation(BaseModel):
    """What a drill implementation saw after injecting its fault."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    terminal: str = Field(min_length=1)
    records: tuple[AuthorityRef, ...] = ()
    checks: tuple[DrillCheck, ...] = ()
    detail: str = ''


class RecoveryDrillResult(BaseModel):
    """One drill's verdict — written into the run report."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    drill_id: str = Field(min_length=1)
    verdict: RecoveryDrillVerdict
    observed_terminal: str = ''
    expected_terminal: str = ''
    checks: tuple[DrillCheck, ...] = ()
    records: tuple[AuthorityRef, ...] = ()
    detail: str = ''
    elapsed_ms: int = Field(default=0, ge=0)


class RecoveryDrillReport(BaseModel):
    """The JSON report emitted by ``drill run`` (also sealed refs)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    format: Literal['htdt-recovery-drill-report-1'] = (
        RECOVERY_DRILL_REPORT_FORMAT)
    document_id: str = Field(default='')
    manifest_sha256: str = Field(min_length=64)
    results: tuple[RecoveryDrillResult, ...]
    overall_verdict: Literal['all_recovered', 'failures_detected']
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)
    report_sha256: str = Field(default='')

    @model_validator(mode='after')
    def _seal(self) -> 'RecoveryDrillReport':
        payload = self.model_dump(mode='json')
        payload['report_sha256'] = ''
        expected = canonical_sha256(payload)
        if not self.report_sha256:
            object.__setattr__(self, 'report_sha256', expected)
        elif self.report_sha256 != expected:
            raise ValueError(
                'recovery drill report_sha256 does not match content')
        return self


class DrillContext(BaseModel):
    """Per-run context handed to implementations."""

    model_config = ConfigDict(frozen=True, extra='forbid', arbitrary_types_allowed=True)

    work_root: Path
    document_id: str = Field(default='')
    clock: Callable[[], str] | None = None


_DRILL_NOW = '2026-10-08T00:00:00Z'


def _now(ctx: DrillContext) -> str:
    if ctx.clock is not None:
        return ctx.clock()
    return _DRILL_NOW


def _sandbox(ctx: DrillContext, spec: RecoveryDrillSpec) -> Path:
    """Per-drill scratch dir — wiped first so reruns are deterministic."""

    path = ctx.work_root / spec.drill_id
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check(
    check_id: str,
    kind: DrillCheckKind,
    passed: bool,
    detail: str = '',
) -> DrillCheck:
    return DrillCheck(
        check_id=check_id, kind=kind, passed=passed, detail=detail)


# ---------------------------------------------------------------------------
# Manifest — the drill matrix as data
# ---------------------------------------------------------------------------


def builtin_drill_manifest() -> RecoveryDrillManifest:
    """The shipped drill matrix, mirrored into
    ``scripts/recovery_drill_manifest.json`` for #903 enumeration."""

    return RecoveryDrillManifest(drills=(
        RecoveryDrillSpec(
            drill_id='session-crash-mid-write',
            title='Session killed mid-write restores dirty state',
            issue_refs=('#883',),
            fault='process_kill_mid_write',
            fault_summary=(
                'A session journal records workspace state, a sealed '
                'scene draft, a pending import and annotation, an '
                'unmatched write intent, a sweep-acquisition stage and '
                'a device-apply operation — then the process dies '
                'without a clean close.'),
            recovery_expectation=(
                'Detection reports ending=forced_termination with all '
                'item kinds recoverable, uncertain external effects '
                'stay DEVICE_STATE_UNKNOWN / ACQUISITION_INCOMPLETE / '
                'WRITE_COMPLETION_UNKNOWN (never inferred), the sealed '
                'detection record exists, and apply_session_restore '
                'completes with the draft bytes provably intact.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='restored',
                expected_records=(
                    'session_recovery_journal',
                    'session_recovery_decision',
                ),
                forbidden_terminals=(
                    'recovered_silently', 'no_report'),
                required_checks=(
                    'ending_classified', 'items_recoverable',
                    'reconciliations_honest', 'draft_bytes_preserved',
                    'detection_recorded', 'restore_completed'),
            ),
            implementation='session_crash_mid_write',
        ),
        RecoveryDrillSpec(
            drill_id='session-journal-torn-tail',
            title='Torn-tail journal recovers prefix, corrupt rejects',
            issue_refs=('#883',),
            fault='journal_tail_truncation',
            fault_summary=(
                'One journal ends mid-line (crash while writing); a '
                'sibling journal has a mid-file corrupt line.'),
            recovery_expectation=(
                'The torn journal reports integrity=tail_torn with the '
                'intact prefix still recoverable (never claimed '
                'intact); the corrupt journal is rejected, not '
                'recovered — fail-closed evidence handling.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='report_emitted',
                expected_records=('session_recovery_journal',),
                forbidden_terminals=('intact_claimed',),
                required_checks=(
                    'tail_torn_honest', 'prefix_recovered',
                    'corrupt_rejected', 'detection_recorded'),
            ),
            implementation='session_journal_torn_tail',
        ),
        RecoveryDrillSpec(
            drill_id='update-crash-mid-swap',
            title='Update killed between swap renames rolls back',
            issue_refs=('#889',),
            fault='kill_mid_swap',
            fault_summary=(
                'After staging, the process dies between the two swap '
                'renames: previous-install holds the old tree, staged '
                'still holds the new payload — a partial swap.'),
            recovery_expectation=(
                'resume() re-derives stage=staged, observes a partial '
                'install and rolls back: outcome rolled_back with '
                'rollback_verified, and the live install returns to '
                'the pre-update bytes. Never reports committed.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='rolled_back',
                expected_records=(
                    'update_session', 'update_outcome'),
                forbidden_terminals=('committed', 'applied', 'staged'),
                required_checks=(
                    'partial_detected', 'rollback_verified',
                    'install_bytes_restored', 'never_committed'),
            ),
            implementation='update_crash_mid_swap',
        ),
        RecoveryDrillSpec(
            drill_id='update-crash-mid-rollback',
            title='Update killed inside rollback resumes rollback',
            issue_refs=('#889',),
            fault='kill_mid_rollback',
            fault_summary=(
                'A failed health check triggers rollback; the process '
                'dies inside restore_install after diverting the '
                'broken install.'),
            recovery_expectation=(
                'resume() re-enters rolling_back (bounded repeat) and '
                'completes the restore — outcome rolled_back with a '
                'verified install tree. rollback_attempts records the '
                're-entry honestly.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='rolled_back',
                expected_records=(
                    'update_session', 'update_outcome'),
                forbidden_terminals=('committed', 'rollback_failed'),
                required_checks=(
                    'rollback_reentered', 'rollback_verified',
                    'install_bytes_restored', 'never_committed'),
            ),
            implementation='update_crash_mid_rollback',
        ),
        RecoveryDrillSpec(
            drill_id='update-health-unverifiable',
            title='Unverifiable post-swap health never commits',
            issue_refs=('#889',),
            fault='health_probe_unverifiable',
            fault_summary=(
                'The swap completes, then the health probe raises '
                '(observation itself failed) instead of reporting.'),
            recovery_expectation=(
                'The service auto-rolls-back on unverifiable health '
                'and surfaces the failure — the session can never '
                'report committed without positive evidence.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='rolled_back',
                expected_records=('update_outcome',),
                forbidden_terminals=('committed',),
                required_checks=(
                    'health_error_surfaced', 'rollback_verified',
                    'never_committed'),
            ),
            implementation='update_health_unverifiable',
        ),
        RecoveryDrillSpec(
            drill_id='diagnostics-bundle-collector-failure',
            title='Bundle export records crashed collector honestly',
            issue_refs=('#884',),
            fault='context_collector_failure',
            fault_summary=(
                'A context provider raises mid-collection while a log '
                'member carries a secret-looking token, an IP and a '
                'user path.'),
            recovery_expectation=(
                'The bundle still builds; manifest.json records the '
                'collector failure in collection_errors (never '
                'silently clean), redactions are applied and no '
                'secret material reaches the archive; member sha256s '
                'verify.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='bundle_built_with_recorded_errors',
                expected_records=('file:diagnostic_bundle',),
                forbidden_terminals=('bundle_clean',),
                required_checks=(
                    'bundle_built', 'collection_errors_recorded',
                    'redactions_applied', 'no_secret_leak',
                    'integrity_verified'),
            ),
            implementation='diagnostics_bundle_collector_failure',
        ),
        RecoveryDrillSpec(
            drill_id='vault-locked-denies-material',
            title='Locked vault denies material, loses no reference',
            issue_refs=('#890',),
            fault='vault_locked',
            fault_summary=(
                'A stored credential becomes unreachable when the '
                'platform vault locks mid-session.'),
            recovery_expectation=(
                'retrieve() raises CredentialAuthRequiredError with an '
                'actionable message, appends no bogus used event, and '
                'the sealed reference survives — after unlock the same '
                'credential_id resolves again.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='material_withheld_then_restored',
                expected_records=(
                    'credential_reference', 'credential_lifecycle_event'),
                forbidden_terminals=('material_returned',),
                required_checks=(
                    'baseline_retrieval', 'retrieval_denied',
                    'message_actionable', 'no_false_used_event',
                    'reference_preserved'),
            ),
            implementation='vault_locked_denies_material',
        ),
        RecoveryDrillSpec(
            drill_id='vault-unavailable-fails-closed',
            title='Unavailable vault refuses store without dangling ref',
            issue_refs=('#890',),
            fault='vault_unavailable',
            fault_summary=(
                'The platform vault is absent entirely — store must '
                'fail before any sealed reference is written.'),
            recovery_expectation=(
                'store_credential raises CredentialAuthRequiredError '
                'and leaves zero reference rows and no stored event — '
                'no dangling evidence claims a credential exists.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='store_refused_no_dangling_ref',
                expected_records=(),
                forbidden_terminals=('credential_stored',),
                required_checks=(
                    'store_refused', 'message_actionable',
                    'no_dangling_reference', 'no_stored_event'),
            ),
            implementation='vault_unavailable_fails_closed',
        ),
        RecoveryDrillSpec(
            drill_id='error-boundary-authority-honesty',
            title='Tampered sealed row classifies AUTHORITY, not success',
            issue_refs=('#815', '#890'),
            fault='sealed_record_tamper',
            fault_summary=(
                'A credential-reference row is mutated directly in '
                'sqlite, then the repository read surfaces it; the '
                'error boundary must classify it as an authority '
                'failure and report an error — never degrade to a '
                'fabricated success.'),
            recovery_expectation=(
                'The store read raises an integrity error, '
                'classify_boundary_error returns AUTHORITY, '
                'report_boundary_failure yields severity error with '
                'the detail, and contrasting user-input/unexpected '
                'faults classify correctly.'),
            expectation=RecoveryDrillExpectation(
                expected_terminal='authority_failures_observable',
                expected_records=(),
                forbidden_terminals=('silent_accept',),
                required_checks=(
                    'tamper_detected', 'classifies_authority',
                    'report_is_error', 'user_input_distinct',
                    'unexpected_distinct'),
            ),
            implementation='error_boundary_authority_honesty',
        ),
    ))


# ---------------------------------------------------------------------------
# Shared update-drill sandbox (mirrors test_issue_889 helpers)
# ---------------------------------------------------------------------------

_UPDATE_PAYLOADS: dict[str, bytes] = {
    'app.bin': b'new-application-bytes-drill',
    'lib.dll': b'new-library-bytes-drill',
    'manifest.json': b'{"release": "9.9.0", "channel": "stable"}',
}
_UPDATE_OLD_APP = b'old-application-bytes'
_UPDATE_OLD_LIB = b'old-library-bytes'


def _update_env(root: Path) -> tuple[Path, Path, Path]:
    from .cad_schema import NATIVE_SCHEMA_VERSION

    install_root = root / 'install'
    data_dir = root / 'data'
    update_area = root / 'update-area'
    install_root.mkdir(parents=True)
    data_dir.mkdir()
    update_area.mkdir()
    (install_root / 'version.json').write_text(
        json.dumps({
            'version': '1.0.0',
            'target_native_schema_version': NATIVE_SCHEMA_VERSION,
        }),
        encoding='utf-8')
    (install_root / 'app.bin').write_bytes(_UPDATE_OLD_APP)
    (install_root / 'lib.dll').write_bytes(_UPDATE_OLD_LIB)
    for name in (
        'preferences.json', 'device-bindings.json', 'schema-metadata.json',
    ):
        (data_dir / name).write_text('{"v": 1}', encoding='utf-8')
    return install_root, data_dir, update_area


def _update_service(
    repository: Any,
    *,
    driver: Any = None,
    probe: Any = None,
    clock: Callable[[], str],
) -> Any:
    from .cad_application_update import (
        ApplicationUpdateService,
        FakeDriverScenario,
        FakePackageScenario,
        FakeUpdateEnvironmentProbe,
        FakeUpdateInstallDriver,
        FakeUpdatePackageSource,
        HealthObservation,
        UpdateCheckResult,
        UpdateEnvironmentFacts,
    )
    from .cad_schema import NATIVE_SCHEMA_VERSION

    if probe is None:
        checks = (
            UpdateCheckResult(
                name='app_launch', status='pass',
                code='launch_ok', reason='launch ok'),
            UpdateCheckResult(
                name='version_stamp', status='pass',
                code='version_stamp_match', reason='observed 9.9.0'),
            UpdateCheckResult(
                name='schema_open', status='pass',
                code='schema_open_ok', reason='schema opened'),
        )
        probe = FakeUpdateEnvironmentProbe(
            UpdateEnvironmentFacts(
                os_name='windows',
                runtime_version='3.12.10',
                app_version='1.0.0',
                app_native_schema_version=NATIVE_SCHEMA_VERSION,
                data_dir_schema_version=NATIVE_SCHEMA_VERSION,
                free_disk_bytes=1_000_000_000,
                open_transaction_kinds=(),
                pending_recovery_sessions=0,
                bound_adapter_ids=(),
                adapter_api_level=3,
            ),
            health=HealthObservation(
                checks=checks,
                observed_version='9.9.0',
                observed_native_schema=NATIVE_SCHEMA_VERSION,
            ),
        )
    return ApplicationUpdateService(
        repository=repository,
        source=FakeUpdatePackageSource(
            FakePackageScenario(payloads=dict(_UPDATE_PAYLOADS))),
        driver=driver or FakeUpdateInstallDriver(FakeDriverScenario()),
        probe=probe,
        clock=clock,
    )


def _open_update_session(
    service: Any,
    ctx: DrillContext,
    sandbox: Path,
    *,
    data_backup_kind: str = 'full',
) -> tuple[Any, Any, Path, Path, Path]:
    from .cad_application_update import (
        UpdateArtifactRef,
        UpdateSignatureState,
    )
    from .cad_schema import NATIVE_SCHEMA_VERSION

    install_root, data_dir, update_area = _update_env(sandbox)
    package_kwargs = dict(
        target_version='9.9.0',
        channel='stable',
        provenance='release-manifest',
        artifacts=tuple(
            UpdateArtifactRef(
                name=name,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                kind=('manifest' if name == 'manifest.json'
                      else 'payload'),
            )
            for name, data in _UPDATE_PAYLOADS.items()),
        signature=UpdateSignatureState(status='unsigned'),
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
    session, package = service.open_session(
        document_id=ctx.document_id,
        install_root=str(install_root),
        data_dir=str(data_dir),
        update_area=str(update_area),
        app_version_before='1.0.0',
        native_schema_before=NATIVE_SCHEMA_VERSION,
        signature_policy='allow_unsigned',
        data_backup_kind=data_backup_kind,
        operator_id='drill-operator',
        **package_kwargs,
    )
    return session, package, install_root, data_dir, update_area


def _drive_to_staged(
    service: Any,
    ctx: DrillContext,
    sandbox: Path,
    *,
    data_backup_kind: str = 'full',
) -> tuple[Any, Path, Path, Path]:
    session, _package, install_root, data_dir, update_area = (
        _open_update_session(
            service, ctx, sandbox, data_backup_kind=data_backup_kind))
    service.fetch_package(session)
    service.verify_package(session)
    report = service.run_preflight(session)
    if report.verdict not in ('eligible', 'eligible_with_warnings'):
        raise RuntimeError(
            f'drill preflight unexpectedly {report.verdict}')
    service.authorize(
        session, scope='apply', actor='drill-operator',
        acknowledges_irreversible_migration=(
            report.irreversible_migration_disclosed))
    service.capture_restore_point(session)
    service.stage_payload(session)
    return session, install_root, data_dir, update_area


def _update_refs(
    repository: Any,
    session: Any,
) -> tuple[AuthorityRef, ...]:
    from .cad_application_update import session_binding

    refs = [session_binding(session)]
    outcome = _outcome_for(repository, session)
    if outcome is not None:
        refs.append(AuthorityRef(
            kind='update_outcome',
            ref_id=outcome.outcome_id,
            ref_sha256=outcome.outcome_sha256))
    return tuple(refs)


def _outcome_for(repository: Any, session: Any) -> Any:
    outcomes = repository.list_outcomes(session.session_id)
    return outcomes[-1] if outcomes else None


# ---------------------------------------------------------------------------
# Drill implementations — each returns a DrillObservation
# ---------------------------------------------------------------------------


def _impl_session_crash_mid_write(ctx: DrillContext,
                                  spec: RecoveryDrillSpec
                                  ) -> DrillObservation:
    from .cad_document import WorkingDocument
    from .cad_repository import SceneRepository
    from .cad_scene import Position3, make_f1_scene
    from .cad_schema import NATIVE_SCHEMA_VERSION
    from .session_recovery import (
        SessionJournal,
        apply_session_restore,
        inspect_recoverable_sessions,
    )
    from .session_recovery_repository import SessionRecoveryRepository

    sandbox = _sandbox(ctx, spec)
    data_dir = sandbox / 'data'
    data_dir.mkdir()
    scene_repo = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    recovery_repo = SessionRecoveryRepository(scene_repo)

    # Seed a real sealed draft (dirty state) + its content hash.
    first = scene_repo.save(make_f1_scene(), parent_revision_id=None).revision
    document_id = first.document_id
    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity(
        'speaker-fl', Position3(x_m=1.9, y_m=0.75, z_m=1.05))
    recovery = scene_repo.save_recovery(
        working.committed_document,
        source_revision_id=working.source_revision_id)
    draft_sha = recovery.content_hash

    # Crash mid-write: journal everything, never close cleanly.
    journal = SessionJournal.open(
        data_dir, session_id='sess-drill', launch_id='launch-drill',
        pid=999_999_999, boot='boot-drill',
        native_schema_version=NATIVE_SCHEMA_VERSION,
        project_document_id=document_id)
    journal.record_workspace_state(
        destination='measurement',
        dirty_documents=(document_id,),
        contexts={'workspace_id': 'measurement'})
    journal.record_scene_draft(
        document_id=document_id, content_hash=draft_sha)
    journal.record_pending_annotation(
        annotation_id='ann-1', document_id=document_id,
        summary='drill annotation', body={'note': 'drill'})
    journal.record_pending_import(
        {'import_id': 'imp-1', 'path': 'a.wav',
         'document_id': document_id})
    journal.record_write_intent(
        intent_id='w-1', path=str(sandbox / 'scratch.bin'),
        document_id=document_id)
    journal.record_acquisition_stage(
        run_id='run-1', stage='capture_started')
    journal.record_operation_started(
        'device_apply_transaction', 'txn-1', document_id=document_id)
    journal.close()  # crash: abnormal close, no clean marker

    # Empty repositories: no sealed evidence exists for the declared
    # apply op or the journaled acquisition — both must stay unknown.
    class _StoreList:
        def list(self, _doc: str) -> tuple:
            return ()

    class _EmptyApplyRepo:
        plans = _StoreList()
        transactions = _StoreList()
        writes = _StoreList()
        verifications = _StoreList()

    class _EmptySweepRepo:
        def get_run_by_run_id(self, _run_id: str) -> None:
            return None

    inspection = inspect_recoverable_sessions(
        data_dir,
        scene_repository=scene_repo,
        session_recovery_repository=recovery_repo,
        apply_repository=_EmptyApplyRepo(),
        sweep_repository=_EmptySweepRepo(),
        pid_alive=lambda pid: False,
        current_boot_token='boot-drill',
        current_native_schema_version=NATIVE_SCHEMA_VERSION,
        current_session_id='sess-current')

    checks: list[DrillCheck] = []
    records: list[AuthorityRef] = []
    report = inspection.reports[0] if inspection.reports else None
    checks.append(_check(
        'ending_classified', 'expectation',
        report is not None
        and report.ending == 'forced_termination',
        f'reports={len(inspection.reports)} '
        f'ending={report.ending if report else None}'))
    item_kinds = {item.kind for item in report.items} if report else set()
    expected_kinds = {
        'scene_draft', 'workspace_state', 'pending_annotation',
        'pending_import', 'project_binding',
    }
    checks.append(_check(
        'items_recoverable', 'preservation',
        expected_kinds <= item_kinds and report is not None and all(
            item.availability == 'available' for item in report.items
            if item.kind in expected_kinds),
        f'kinds={sorted(item_kinds)}'))
    rec_states = {
        rec.state for rec in (report.reconciliations if report
                              else ())}
    honest_states = {
        'DEVICE_STATE_UNKNOWN', 'ACQUISITION_INCOMPLETE',
        'WRITE_COMPLETION_UNKNOWN'}
    checks.append(_check(
        'reconciliations_honest', 'honesty',
        report is not None and honest_states <= rec_states and all(
            rec.resolution is None or 'success' not in rec.resolution
            for rec in report.reconciliations),
        f'states={sorted(rec_states)}'))
    snapshot = scene_repo.recovery(document_id)
    draft_ok = (
        snapshot is not None and snapshot.content_hash == draft_sha)
    checks.append(_check(
        'draft_bytes_preserved', 'preservation', draft_ok,
        f'draft_sha={draft_sha[:16]}'))
    record = recovery_repo.journal_record_for_session('sess-drill')
    checks.append(_check(
        'detection_recorded', 'expectation', record is not None,
        f'journal_id={record.journal_id if record else None}'))
    if record is not None:
        records.append(AuthorityRef(
            kind='session_recovery_journal',
            ref_id=record.journal_id,
            ref_sha256=record.journal_sha256))

    restored_ok = False
    if report is not None:
        apply_session_restore(
            recovery_repo, report,
            restoring_session_id='sess-current',
            actor='drill')
        restored_ok = recovery_repo.session_is_resolved('sess-drill')
        decision = recovery_repo.decisions_for_session('sess-drill')
        for d in decision:
            records.append(AuthorityRef(
                kind='session_recovery_decision',
                ref_id=d.decision_id,
                ref_sha256=d.decision_sha256))
    checks.append(_check(
        'restore_completed', 'expectation', restored_ok,
        'session resolved' if restored_ok else 'not resolved'))

    terminal = 'restored' if restored_ok else 'no_report'
    return DrillObservation(
        terminal=terminal, records=tuple(records),
        checks=tuple(checks),
        detail=f'ending={report.ending if report else "none"}')


def _impl_session_journal_torn_tail(ctx: DrillContext,
                                    spec: RecoveryDrillSpec
                                    ) -> DrillObservation:
    from .cad_repository import SceneRepository
    from .cad_schema import NATIVE_SCHEMA_VERSION
    from .session_recovery import (
        SessionJournal,
        inspect_journal_dir,
        inspect_recoverable_sessions,
    )
    from .session_recovery_repository import SessionRecoveryRepository

    sandbox = _sandbox(ctx, spec)
    data_dir = sandbox / 'data'
    data_dir.mkdir()
    scene_repo = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    recovery_repo = SessionRecoveryRepository(scene_repo)

    torn = SessionJournal.open(
        data_dir, session_id='sess-torn', launch_id='launch-t',
        pid=999_999_999, boot='boot-drill',
        native_schema_version=NATIVE_SCHEMA_VERSION)
    torn.record_workspace_state(
        destination='room', contexts={'workspace_id': 'room'})
    torn.record_operation_started('sweep_acquisition', 'run-x')
    torn.close()
    torn_dir = data_dir / 'session-journal' / 'sess-torn'
    journal_path = torn_dir / 'journal.jsonl'
    lines = journal_path.read_text(encoding='utf-8').splitlines()
    # Crash mid-write: a truncated final line with no newline.
    journal_path.write_text(
        '\n'.join(lines) + '\n{"entry_id":"e-torn","kind":"heartb',
        encoding='utf-8')

    corrupt = SessionJournal.open(
        data_dir, session_id='sess-corrupt', launch_id='launch-c',
        pid=999_999_999, boot='boot-drill',
        native_schema_version=NATIVE_SCHEMA_VERSION)
    corrupt.record_workspace_state(
        destination='room', contexts={'workspace_id': 'room'})
    corrupt.record_pending_import(
        {'import_id': 'imp-x', 'path': 'b.wav'})
    corrupt.close()
    corrupt_dir = data_dir / 'session-journal' / 'sess-corrupt'
    corrupt_path = corrupt_dir / 'journal.jsonl'
    raw = corrupt_path.read_text(encoding='utf-8').splitlines()
    # Mid-file corruption is never a torn tail — it must be rejected.
    assert len(raw) >= 3, 'expected >=3 journal lines for mid-file cut'
    raw[1] = '{"entry_id":"e-bad","kind":"workspace_snapshot","seq":1'
    corrupt_path.write_text('\n'.join(raw) + '\n', encoding='utf-8')

    torn_inspection = inspect_journal_dir(torn_dir)
    inspection = inspect_recoverable_sessions(
        data_dir,
        scene_repository=scene_repo,
        session_recovery_repository=recovery_repo,
        pid_alive=lambda pid: False,
        current_boot_token='boot-drill',
        current_native_schema_version=NATIVE_SCHEMA_VERSION,
        current_session_id='sess-current')

    checks: list[DrillCheck] = []
    records: list[AuthorityRef] = []
    torn_report = next(
        (r for r in inspection.reports
         if r.session_id == 'sess-torn'), None)
    checks.append(_check(
        'tail_torn_honest', 'honesty',
        torn_report is not None
        and torn_report.integrity == 'tail_torn'
        and torn_inspection.verdict == 'tail_torn',
        f'integrity={torn_report.integrity if torn_report else None}'))
    prefix_len = len(lines)
    checks.append(_check(
        'prefix_recovered', 'preservation',
        len(torn_inspection.entries) == prefix_len,
        f'entries={len(torn_inspection.entries)} '
        f'expected={prefix_len}'))
    checks.append(_check(
        'corrupt_rejected', 'expectation',
        any('sess-corrupt' in (r.session_id or '')
            or Path(r.journal_dir).name == 'sess-corrupt'
            for r in inspection.rejected),
        f'rejected={[r.session_id for r in inspection.rejected]}'))
    record = recovery_repo.journal_record_for_session('sess-torn')
    checks.append(_check(
        'detection_recorded', 'expectation', record is not None,
        f'journal_id={record.journal_id if record else None}'))
    if record is not None:
        records.append(AuthorityRef(
            kind='session_recovery_journal',
            ref_id=record.journal_id,
            ref_sha256=record.journal_sha256))

    terminal = 'report_emitted' if torn_report is not None else 'no_report'
    return DrillObservation(
        terminal=terminal, records=tuple(records),
        checks=tuple(checks))


def _impl_update_crash_mid_swap(ctx: DrillContext,
                                spec: RecoveryDrillSpec
                                ) -> DrillObservation:
    from .cad_application_update_repository import (
        CadApplicationUpdateRepository)
    from .cad_repository import SceneRepository

    sandbox = _sandbox(ctx, spec)
    repository = CadApplicationUpdateRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))
    service = _update_service(repository, clock=lambda: _now(ctx))
    session, install_root, data_dir, update_area = _drive_to_staged(
        service, ctx, sandbox)

    # The kill: first swap rename only — previous-install holds the old
    # tree, staged still holds the new payload (partial swap).
    install_root.rename(update_area / 'previous-install')

    resumed = _update_service(repository, clock=lambda: _now(ctx))
    state = resumed.resume(session)

    transitions = repository.list_transitions(session.session_id)
    outcome = _outcome_for(repository, session)
    checks: list[DrillCheck] = []
    checks.append(_check(
        'partial_detected', 'expectation',
        any('partial' in t.reason
            for t in transitions
            if t.event_kind == 'rollback_started'),
        'rollback triggered by resume_partial_swap_detected'))
    rb_verified = bool(
        outcome is not None and outcome.verdict == 'rolled_back'
        and outcome.rollback_verified)
    checks.append(_check(
        'rollback_verified', 'expectation', rb_verified,
        f'verdict={outcome.verdict if outcome else None}'))
    restored = (
        install_root.is_dir()
        and (install_root / 'app.bin').read_bytes() == _UPDATE_OLD_APP
        and (install_root / 'lib.dll').read_bytes() == _UPDATE_OLD_LIB)
    checks.append(_check(
        'install_bytes_restored', 'preservation', restored,
        'install tree bytes vs pre-update payload'))
    checks.append(_check(
        'never_committed', 'honesty',
        outcome is not None and outcome.verdict != 'committed'
        and state.current_stage != 'committed',
        f'verdict={outcome.verdict if outcome else None}'))
    return DrillObservation(
        terminal=state.current_stage,
        records=_update_refs(repository, session),
        checks=tuple(checks))


def _impl_update_crash_mid_rollback(ctx: DrillContext,
                                    spec: RecoveryDrillSpec
                                    ) -> DrillObservation:
    from .cad_application_update import (
        FakeDriverScenario,
        FakeUpdateEnvironmentProbe,
        FakeUpdateInstallDriver,
        UpdateDriverError,
    )
    from .cad_application_update_repository import (
        CadApplicationUpdateRepository)
    from .cad_repository import SceneRepository

    sandbox = _sandbox(ctx, spec)
    repository = CadApplicationUpdateRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))

    class _SimulatedHalt(BaseException):
        """A kill, not a catchable error — the service never sees it."""

    class _HaltMidRestore(FakeUpdateInstallDriver):
        def restore_install(self, **kwargs: Any) -> Any:
            update_area = kwargs['update_area']
            install_root = kwargs['install_root']
            failed_index = 0
            while (update_area / f'failed-install-{failed_index}'
                   ).exists():
                failed_index += 1
            if install_root.exists():
                install_root.rename(
                    update_area / f'failed-install-{failed_index}')
            raise _SimulatedHalt('simulated kill mid-restore')

    # Unhealthy post-swap probe so the service enters rollback, where the
    # driver kills the "process" mid-restore.
    from .cad_application_update import (
        HealthObservation, UpdateCheckResult, UpdateEnvironmentFacts)
    from .cad_schema import NATIVE_SCHEMA_VERSION

    unhealthy_probe = FakeUpdateEnvironmentProbe(
        UpdateEnvironmentFacts(
            os_name='windows', runtime_version='3.12.10',
            app_version='1.0.0',
            app_native_schema_version=NATIVE_SCHEMA_VERSION,
            data_dir_schema_version=NATIVE_SCHEMA_VERSION,
            free_disk_bytes=1_000_000_000,
            open_transaction_kinds=(),
            pending_recovery_sessions=0,
            bound_adapter_ids=(), adapter_api_level=3),
        health=HealthObservation(
            checks=(UpdateCheckResult(
                name='app_launch', status='fail',
                code='launch_failed', reason='swap broke launch'),),
            observed_version='9.9.0',
            observed_native_schema=NATIVE_SCHEMA_VERSION))
    service = _update_service(
        repository, driver=_HaltMidRestore(FakeDriverScenario()),
        probe=unhealthy_probe, clock=lambda: _now(ctx))
    session, install_root, data_dir, update_area = _drive_to_staged(
        service, ctx, sandbox)
    service.apply_swap(session)
    report = service.health_check(session)
    if report.verdict in ('healthy', 'healthy_with_warnings'):
        raise RuntimeError('drill health probe reported healthy')
    halt_seen = False
    try:
        service.rollback(session, reason=f'drill_{report.verdict}')
    except _SimulatedHalt:
        halt_seen = True

    resumed = _update_service(repository, clock=lambda: _now(ctx))
    state = resumed.resume(session)

    outcome = _outcome_for(repository, session)
    checks: list[DrillCheck] = []
    checks.append(_check(
        'rollback_reentered', 'expectation',
        halt_seen and state.rollback_attempts >= 2,
        f'halt={halt_seen} attempts={state.rollback_attempts}'))
    rb_verified = bool(
        outcome is not None and outcome.verdict == 'rolled_back'
        and outcome.rollback_verified)
    checks.append(_check(
        'rollback_verified', 'expectation', rb_verified,
        f'verdict={outcome.verdict if outcome else None}'))
    restored = (
        install_root.is_dir()
        and (install_root / 'app.bin').read_bytes() == _UPDATE_OLD_APP
        and (install_root / 'lib.dll').read_bytes() == _UPDATE_OLD_LIB)
    checks.append(_check(
        'install_bytes_restored', 'preservation', restored,
        'install tree bytes vs pre-update payload'))
    checks.append(_check(
        'never_committed', 'honesty',
        outcome is not None and outcome.verdict != 'committed',
        f'verdict={outcome.verdict if outcome else None}'))
    return DrillObservation(
        terminal=state.current_stage,
        records=_update_refs(repository, session),
        checks=tuple(checks))


def _impl_update_health_unverifiable(ctx: DrillContext,
                                     spec: RecoveryDrillSpec
                                     ) -> DrillObservation:
    from .cad_application_update import (
        ApplicationUpdateError,
        FakeUpdateEnvironmentProbe,
        HealthObservation, UpdateCheckResult, UpdateEnvironmentFacts)
    from .cad_application_update_repository import (
        CadApplicationUpdateRepository)
    from .cad_repository import SceneRepository
    from .cad_schema import NATIVE_SCHEMA_VERSION

    sandbox = _sandbox(ctx, spec)
    repository = CadApplicationUpdateRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))
    probe = FakeUpdateEnvironmentProbe(
        UpdateEnvironmentFacts(
            os_name='windows', runtime_version='3.12.10',
            app_version='1.0.0',
            app_native_schema_version=NATIVE_SCHEMA_VERSION,
            data_dir_schema_version=NATIVE_SCHEMA_VERSION,
            free_disk_bytes=1_000_000_000,
            open_transaction_kinds=(),
            pending_recovery_sessions=0,
            bound_adapter_ids=(), adapter_api_level=3),
        health_error=ApplicationUpdateError(
            'health observation unavailable'))
    service = _update_service(
        repository, probe=probe, clock=lambda: _now(ctx))
    session, install_root, data_dir, update_area = _drive_to_staged(
        service, ctx, sandbox)
    service.apply_swap(session)
    surfaced = False
    try:
        service.health_check(session)
    except ApplicationUpdateError:
        surfaced = True

    state = resumed_state = None
    resumed = _update_service(repository, clock=lambda: _now(ctx))
    state = resumed.resume(session)

    outcome = _outcome_for(repository, session)
    checks: list[DrillCheck] = []
    checks.append(_check(
        'health_error_surfaced', 'expectation', surfaced,
        'probe failure propagated'))
    rb_verified = bool(
        outcome is not None and outcome.verdict == 'rolled_back'
        and outcome.rollback_verified)
    checks.append(_check(
        'rollback_verified', 'expectation', rb_verified,
        f'verdict={outcome.verdict if outcome else None}'))
    checks.append(_check(
        'never_committed', 'honesty',
        outcome is not None and outcome.verdict != 'committed'
        and state.current_stage != 'committed',
        f'verdict={outcome.verdict if outcome else None}'))
    return DrillObservation(
        terminal=state.current_stage,
        records=_update_refs(repository, session),
        checks=tuple(checks))


def _impl_diagnostics_bundle_collector_failure(
        ctx: DrillContext, spec: RecoveryDrillSpec) -> DrillObservation:
    from .support_diagnostics import (
        DiagnosticPackageBuilder, PackageCategory)

    sandbox = _sandbox(ctx, spec)
    data_dir = sandbox / 'data'
    diag = data_dir / 'diagnostics'
    diag.mkdir(parents=True)
    (diag / 'htdt-native.log').write_text(
        'INFO open C:\\Users\\FieldTech\\proj from 10.0.0.5 '
        'token=abc123DEF456\n',
        encoding='utf-8')

    def _boom() -> dict:
        raise RuntimeError('gpu probe exploded')

    builder = DiagnosticPackageBuilder(
        data_dir,
        context_providers={
            PackageCategory.GPU_CONTEXT: _boom,
            PackageCategory.WORKFLOW_STATE: lambda: {
                'hostname': 'CUSTOMER-HT-PC',
            },
        })
    result = builder.build(sandbox / 'bundle.zip', builder.plan())

    checks: list[DrillCheck] = []
    records: list[AuthorityRef] = []
    import zipfile

    built = result.path.is_file()
    checks.append(_check(
        'bundle_built', 'expectation', built,
        f'path={result.path}'))
    manifest: dict[str, Any] = {}
    if built:
        with zipfile.ZipFile(result.path) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read('manifest.json'))
            log = archive.read('logs/htdt-native.log').decode('utf-8')
            integrity_ok = all(
                _sha_ok(archive, name, sha)
                for name, sha in manifest['integrity'].items())
            blob = b''.join(archive.read(n) for n in names)
        checks.append(_check(
            'collection_errors_recorded', 'honesty',
            'gpu_context: RuntimeError' in manifest['collection_errors'],
            f"errors={manifest['collection_errors']}"))
        log_member = manifest['members'].get('logs/htdt-native.log', {})
        checks.append(_check(
            'redactions_applied', 'honesty',
            log_member.get('redactions', {}).get('hosts', 0) >= 1
            and 'FieldTech' not in log and '10.0.0.5' not in log,
            f"redactions={log_member.get('redactions')}"))
        checks.append(_check(
            'no_secret_leak', 'honesty',
            b'abc123DEF456' not in blob and b'CUSTOMER-HT-PC' not in blob,
            'secret token/hostname absent from every member'))
        checks.append(_check(
            'integrity_verified', 'preservation', integrity_ok,
            'manifest member sha256s'))
        records.append(AuthorityRef(
            kind='file:diagnostic_bundle',
            ref_id=str(result.path),
            ref_sha256=_sha256_file(result.path)))
    else:
        for cid in ('collection_errors_recorded', 'redactions_applied',
                    'no_secret_leak', 'integrity_verified'):
            checks.append(_check(
                cid, 'preservation', False, 'bundle missing'))

    terminal = ('bundle_built_with_recorded_errors' if built
                else 'bundle_missing')
    return DrillObservation(
        terminal=terminal, records=tuple(records),
        checks=tuple(checks))


def _sha_ok(archive: Any, name: str, sha: str) -> bool:
    return hashlib.sha256(archive.read(name)).hexdigest() == sha


def _impl_vault_locked_denies_material(ctx: DrillContext,
                                       spec: RecoveryDrillSpec
                                       ) -> DrillObservation:
    from .cad_credential_vault import (
        CredentialAuthRequiredError,
        CredentialVaultService,
        MemoryCredentialVault)
    from .cad_credential_vault_repository import (
        CadCredentialVaultRepository)
    from .cad_repository import SceneRepository

    sandbox = _sandbox(ctx, spec)
    repository = CadCredentialVaultRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))
    vault = MemoryCredentialVault()
    service = CredentialVaultService(repository, vault)
    doc = ctx.document_id or 'doc-drill'
    service.record_consent(doc, actor='drill', note='drill consent')
    ref = service.store_credential(
        doc, scope_kind='device', scope_ref='avr_lan',
        credential_type='password', material='s3cret',
        actor='drill')

    baseline = service.retrieve(ref.credential_id, actor='drill')
    baseline_ok = baseline.reveal() == 's3cret'
    events_before = repository.list_events(
        doc, credential_id=ref.credential_id)

    vault.set_locked(True)
    denied_error: str | None = None
    try:
        service.retrieve(ref.credential_id, actor='drill')
    except CredentialAuthRequiredError as exc:
        denied_error = str(exc)
    events_locked = repository.list_events(
        doc, credential_id=ref.credential_id)

    vault.set_locked(False)
    restored = service.retrieve(ref.credential_id, actor='drill')
    preserved = repository.current_reference(ref.credential_id)

    checks = (
        _check('baseline_retrieval', 'expectation', baseline_ok,
               'unlocked retrieval returns material'),
        _check('retrieval_denied', 'expectation',
               denied_error is not None,
               f'denied={denied_error is not None}'),
        _check('message_actionable', 'honesty',
               denied_error is not None
               and ('lock' in denied_error.lower()
                    or 'unlock' in denied_error.lower()),
               f'message={denied_error!r}'),
        _check('no_false_used_event', 'honesty',
               [e.event_kind for e in events_locked]
               == [e.event_kind for e in events_before],
               'no used event appended under lock'),
        _check('reference_preserved', 'preservation',
               preserved is not None and restored.reveal() == 's3cret',
               'reference row survives lock; material returns after unlock'),
    )
    records = [
        AuthorityRef(
            kind='credential_reference',
            ref_id=ref.reference_id,
            ref_sha256=ref.reference_sha256),
    ]
    for event in repository.list_events(
            doc, credential_id=ref.credential_id):
        records.append(AuthorityRef(
            kind='credential_lifecycle_event',
            ref_id=event.event_id,
            ref_sha256=event.event_sha256))
    terminal = (
        'material_withheld_then_restored'
        if denied_error is not None and restored.reveal() == 's3cret'
        else 'material_returned' if denied_error is None
        else 'material_lost')
    return DrillObservation(
        terminal=terminal, records=tuple(records), checks=checks)


def _impl_vault_unavailable_fails_closed(ctx: DrillContext,
                                         spec: RecoveryDrillSpec
                                         ) -> DrillObservation:
    from .cad_credential_vault import (
        CredentialAuthRequiredError,
        CredentialVaultService,
        UnavailableCredentialVault)
    from .cad_credential_vault_repository import (
        CadCredentialVaultRepository)
    from .cad_repository import SceneRepository

    sandbox = _sandbox(ctx, spec)
    repository = CadCredentialVaultRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))
    service = CredentialVaultService(
        repository, UnavailableCredentialVault())
    doc = ctx.document_id or 'doc-drill'
    service.record_consent(doc, actor='drill', note='drill consent')
    denied_error: str | None = None
    try:
        service.store_credential(
            doc, scope_kind='device', scope_ref='avr_lan',
            credential_type='password', material='s3cret',
            actor='drill')
    except CredentialAuthRequiredError as exc:
        denied_error = str(exc)
    refs = repository.list_references(doc)
    events = repository.list_events(doc)
    stored_events = [e for e in events if e.event_kind == 'stored']

    checks = (
        _check('store_refused', 'expectation', denied_error is not None,
               f'denied={denied_error is not None}'),
        _check('message_actionable', 'honesty',
               denied_error is not None
               and 'unavailable' in denied_error.lower(),
               f'message={denied_error!r}'),
        _check('no_dangling_reference', 'preservation',
               len(refs) == 0,
               f'references={len(refs)}'),
        _check('no_stored_event', 'honesty',
               len(stored_events) == 0,
               f'stored_events={len(stored_events)}'),
    )
    terminal = ('store_refused_no_dangling_ref'
                if denied_error is not None and not refs
                else 'credential_stored')
    return DrillObservation(
        terminal=terminal, records=(), checks=checks)


def _impl_error_boundary_authority_honesty(
        ctx: DrillContext, spec: RecoveryDrillSpec) -> DrillObservation:
    from .cad_credential_vault import (
        CredentialVaultService, MemoryCredentialVault)
    from .cad_credential_vault_repository import (
        CadCredentialVaultRepository)
    from .cad_repository import SceneRepository
    from .cad_schema import connect_sqlite
    from .error_boundary import (
        BoundaryCategory, UserFacingError,
        classify_boundary_error, is_authority_failure,
        report_boundary_failure)

    sandbox = _sandbox(ctx, spec)
    repository = CadCredentialVaultRepository(
        SceneRepository(sandbox / 'cad-scenes.sqlite3'))
    service = CredentialVaultService(repository, MemoryCredentialVault())
    doc = ctx.document_id or 'doc-drill'
    service.record_consent(doc, actor='drill')
    ref = service.store_credential(
        doc, scope_kind='device', scope_ref='avr_lan',
        credential_type='password', material='s3cret',
        actor='drill')

    # Tamper: flip a persisted column under the payload's feet.
    with closing(connect_sqlite(repository.path)) as conn, conn:
        conn.execute(
            'UPDATE cad_credential_references SET state=? '
            'WHERE reference_id=?', ('tampered', ref.reference_id))
    raised: Exception | None = None
    try:
        repository.get_reference(ref.reference_id)
    except Exception as exc:  # noqa: BLE001 — observing the type
        raised = exc

    category = (classify_boundary_error(raised)
                if raised is not None else None)
    reported: UserFacingError | None = (
        report_boundary_failure(raised, operation='recovery drill')
        if raised is not None else None)

    checks = (
        _check('tamper_detected', 'expectation',
               raised is not None and type(raised).__name__.endswith(
                   'IntegrityError'),
               f'raised={type(raised).__name__ if raised else None}'),
        _check('classifies_authority', 'honesty',
               raised is not None and is_authority_failure(raised)
               and category is BoundaryCategory.AUTHORITY,
               f'category={category}'),
        _check('report_is_error', 'honesty',
               reported is not None
               and reported.severity == 'error',
               f'severity={reported.severity if reported else None}'),
        _check('user_input_distinct', 'expectation',
               classify_boundary_error(
                   ValueError('bad input'))
               is BoundaryCategory.USER_INPUT),
        _check('unexpected_distinct', 'expectation',
               classify_boundary_error(
                   TypeError('bugs'))
               is BoundaryCategory.UNEXPECTED),
    )
    terminal = ('authority_failures_observable'
                if category is BoundaryCategory.AUTHORITY
                else 'silent_accept')
    return DrillObservation(terminal=terminal, records=(), checks=checks)


#: implementation key -> callable(ctx, spec) -> DrillObservation
_IMPLEMENTATIONS: dict[
    str, Callable[[DrillContext, RecoveryDrillSpec], DrillObservation]
] = {
    'session_crash_mid_write': _impl_session_crash_mid_write,
    'session_journal_torn_tail': _impl_session_journal_torn_tail,
    'update_crash_mid_swap': _impl_update_crash_mid_swap,
    'update_crash_mid_rollback': _impl_update_crash_mid_rollback,
    'update_health_unverifiable': _impl_update_health_unverifiable,
    'diagnostics_bundle_collector_failure': (
        _impl_diagnostics_bundle_collector_failure),
    'vault_locked_denies_material': _impl_vault_locked_denies_material,
    'vault_unavailable_fails_closed': _impl_vault_unavailable_fails_closed,
    'error_boundary_authority_honesty': (
        _impl_error_boundary_authority_honesty),
}


def _validate_manifest(manifest: RecoveryDrillManifest) -> None:
    ids = [d.drill_id for d in manifest.drills]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate drill_id in manifest')
    unknown = [
        d.implementation for d in manifest.drills
        if d.implementation not in _IMPLEMENTATIONS]
    if unknown:
        raise ValueError(
            f'manifest references unknown implementations: {unknown}')


_validate_manifest(builtin_drill_manifest())


# ---------------------------------------------------------------------------
# Evaluation + runner
# ---------------------------------------------------------------------------


def evaluate_drill(
    spec: RecoveryDrillSpec,
    observation: DrillObservation,
) -> RecoveryDrillResult:
    """Map an observation onto the verdict lattice — fail closed."""

    exp = spec.expectation
    checks = list(observation.checks)
    emitted = {c.check_id for c in checks}
    missing = [c for c in exp.required_checks if c not in emitted]
    if missing:
        return RecoveryDrillResult(
            drill_id=spec.drill_id, verdict='harness_error',
            observed_terminal=observation.terminal,
            expected_terminal=exp.expected_terminal,
            checks=tuple(checks), records=observation.records,
            detail=f'missing required checks: {missing}')

    terminal = observation.terminal
    verdict: RecoveryDrillVerdict
    detail = observation.detail
    if terminal != exp.expected_terminal:
        if terminal in exp.forbidden_terminals:
            verdict = 'false_success'
            detail = (detail + f' [forbidden terminal {terminal}]').strip()
        else:
            verdict = 'loss_detected'
            detail = (detail
                      + f' [expected terminal {exp.expected_terminal}, '
                      f'observed {terminal}]').strip()
    else:
        record_kinds = {r.kind for r in observation.records}
        missing_records = [
            k for k in exp.expected_records if k not in record_kinds]
        failed_honesty = [
            c.check_id for c in checks
            if c.kind == 'honesty' and not c.passed]
        failed_other = [
            c.check_id for c in checks
            if c.kind != 'honesty' and not c.passed]
        if failed_honesty:
            verdict = 'false_success'
            detail = (detail
                      + f' [honesty checks failed: {failed_honesty}]'
                      ).strip()
        elif missing_records or failed_other:
            verdict = 'loss_detected'
            bits = []
            if missing_records:
                bits.append(f'missing records {missing_records}')
            if failed_other:
                bits.append(f'checks failed {failed_other}')
            detail = (detail + f' [{"; ".join(bits)}]').strip()
        else:
            verdict = 'recovered_as_designed'

    return RecoveryDrillResult(
        drill_id=spec.drill_id, verdict=verdict,
        observed_terminal=terminal,
        expected_terminal=exp.expected_terminal,
        checks=tuple(checks), records=observation.records,
        detail=detail)


def run_drill(
    spec: RecoveryDrillSpec,
    ctx: DrillContext,
) -> RecoveryDrillResult:
    """Execute one drill and evaluate it; harness faults never judge."""

    started = time.monotonic()
    implementation = _IMPLEMENTATIONS.get(spec.implementation)
    if implementation is None:
        return RecoveryDrillResult(
            drill_id=spec.drill_id, verdict='harness_error',
            expected_terminal=spec.expectation.expected_terminal,
            detail=f'no implementation {spec.implementation}')
    try:
        observation = implementation(ctx, spec)
    except Exception as exc:  # error-boundary: drill crashed mid-fault
        return RecoveryDrillResult(
            drill_id=spec.drill_id, verdict='harness_error',
            expected_terminal=spec.expectation.expected_terminal,
            detail=f'{type(exc).__name__}: {exc}',
            elapsed_ms=int((time.monotonic() - started) * 1000))
    result = evaluate_drill(spec, observation)
    return result.model_copy(
        update={'elapsed_ms': int((time.monotonic() - started) * 1000)})


def select_drills(
    manifest: RecoveryDrillManifest,
    drill_ids: tuple[str, ...] | None = None,
) -> tuple[RecoveryDrillSpec, ...]:
    """Resolve ``--drill-id`` selections — unknown ids fail closed."""

    if not drill_ids:
        return manifest.drills
    wanted = set(drill_ids)
    unknown = wanted - {d.drill_id for d in manifest.drills}
    if unknown:
        raise ValueError(
            f'unknown drill ids: {sorted(unknown)}')
    return tuple(
        d for d in manifest.drills if d.drill_id in wanted)


def run_drill_manifest(
    manifest: RecoveryDrillManifest,
    ctx: DrillContext,
    *,
    drill_ids: tuple[str, ...] | None = None,
    started_at_utc: str,
    finished_at_utc: str,
    elapsed_ms: int,
) -> RecoveryDrillReport:
    """Run the selected drills and seal the report."""

    selected = select_drills(manifest, drill_ids)
    results = [run_drill(spec, ctx) for spec in selected]
    overall = (
        'all_recovered' if all(
            r.verdict == 'recovered_as_designed' for r in results)
        else 'failures_detected')
    return RecoveryDrillReport(
        document_id=ctx.document_id,
        manifest_sha256=manifest.manifest_sha256,
        results=tuple(results),
        overall_verdict=overall,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        elapsed_ms=elapsed_ms)


__all__ = [
    'DrillCheck',
    'DrillContext',
    'DrillObservation',
    'FAILING_DRILL_VERDICTS',
    'RECOVERY_DRILL_MANIFEST_FORMAT',
    'RECOVERY_DRILL_REPORT_FORMAT',
    'RecoveryDrillExpectation',
    'RecoveryDrillManifest',
    'RecoveryDrillReport',
    'RecoveryDrillResult',
    'RecoveryDrillSpec',
    'RecoveryDrillVerdict',
    'builtin_drill_manifest',
    'evaluate_drill',
    'run_drill',
    'run_drill_manifest',
    'select_drills',
]
