"""Presentation review/proposal export runner — off the UI thread (#985).

Moves ``build_review_package`` / ``build_proposal_package`` off the Qt
event loop onto a bounded ``NativeWorkerPool`` lane — the same pool
contract the project-bundle export and the support health check already
use — and adds:

- ActivityCenter registration as ``presentation.export.review`` /
  ``presentation.export.proposal`` with *measured* stage/frames/sheets/
  bytes progress — no estimated percentages or ETAs;
- a pinned job record: the sealed session, the source revision, the
  render settings and the output folder are resolved on the UI thread
  before start, so a mid-run re-selection cannot re-attribute results;
- staged-then-atomic publication: the builder writes into a sibling
  ``.<name>.staging-*`` directory, the worker re-hashes it through the
  package verifier, and only then does the UI thread mark the commit
  point and rename it into place — cancel/failure/kill/disk-full never
  leaves a folder that looks like a finished package;
- cooperative cancel: the ActivityCenter ``CANCEL_UNTIL_COMMIT`` request
  flips the worker's cancel flag and the builder raises
  ``ExportCancelledError`` at its next work item;
- superseded-input honesty: when the pinned scene revision is no longer
  the document head at completion, the record is reclassified
  ``COMPLETED_FOR_HISTORICAL_INPUT`` (never silently "current");
- a ``[diag: ...]`` correlation id on failure (#962 support bundle);
- a busy gate — double-clicks cannot spawn parallel builds.

VTK/Qt thread affinity: the ``OffscreenSceneRenderer`` (or the injected
test renderer) is constructed *inside* the worker lane via
``renderer_factory`` and never touches a live ``QtInteractor`` from a
non-UI thread.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from PySide6.QtCore import QObject, Signal

from .activity_center import (
    ActivityCenter,
    Cancellability,
    NavigationPolicy,
    OperationClass,
    OperationProgress,
    OperationState,
    ProgressKind,
    RetryPolicy,
)
from .cad_design_comparison_repository import CadDesignComparisonRepository
from .cad_design_decision_repository import CadDesignDecisionRepository
from .cad_presentation_repository import CadPresentationRepository
from .cad_presentation_session import PresentationSession
from .cad_proposal_package import (
    ProposalPackageResult,
    build_proposal_package,
    verify_proposal_package,
)
from .cad_repository import SceneRepository
from .cad_review_package import (
    OffscreenSceneRenderer,
    ReviewPackageResult,
    build_review_package,
    verify_review_package,
)
from .native_worker import NativeWorkerPool, WORKER_CANCELLED
from .package_progress import (
    ExportCancelledError,
    PackageBuildProgress,
)
from .support_diagnostics import failure_correlation_id
from .user_facing_error import operation_error_message
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId

logger = logging.getLogger(__name__)

ExportKind = Literal['review', 'proposal']

_KIND_LABEL = {'review': 'レビューパッケージ', 'proposal': '提案パッケージ'}
# Stage index the builder leaves for the caller (verify) — see the
# builder docstrings; review counts 6 stages, proposal 5.
_VERIFY_STAGE = {'review': 6, 'proposal': 5}


@dataclass(frozen=True, slots=True)
class PresentationExportJob:
    """Everything the export needs, pinned before the job starts (#985).

    Nothing inside the worker consults live UI state: the sealed
    session, the source revision, the render intent, the output folder
    and the expected work items are all resolved first.
    """

    kind: ExportKind
    session: PresentationSession
    #: Final publication target — kept empty/absent until the verified
    #: staged package is renamed onto it on the UI thread.
    package_dir: Path
    #: Parent the sibling staging directory is created under (same
    #: filesystem, so the publish rename is atomic).
    output_root: Path
    yaw_steps_deg: tuple[int, ...] | None
    include_drawings: bool
    expected_frames: int
    expected_sheets: int


@dataclass(frozen=True, slots=True)
class _BuiltPackage:
    """Worker-lane result: a fully built + verified staged package."""

    job: PresentationExportJob
    staging_dir: Path
    result: ReviewPackageResult | ProposalPackageResult


def _default_renderer_factory(session: PresentationSession) -> object:
    """Create the real offscreen renderer *inside the worker lane*.

    Called on the worker thread — never on the UI thread and never
    against a live ``QtInteractor`` (VTK/Qt affinity rule).
    """
    return OffscreenSceneRenderer(
        session.render.image_width_px,
        session.render.image_height_px,
    )


def _publish_staged(staging_dir: Path, package_dir: Path) -> None:
    """Atomically move a verified staged package onto the final path.

    ``os.replace`` over a same-filesystem sibling is one rename; the
    destination may exist as an empty directory (allowed by
    ``validate_output_target``) and is removed first. A non-empty
    destination raises — the staging tree is then discarded by the
    caller, so no partial or stale content survives under either name.
    """
    if package_dir.exists():
        package_dir.rmdir()  # empty only — raises honestly otherwise
    os.replace(staging_dir, package_dir)


def _bytes_label(count: int) -> str:
    value = float(count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024.0 or unit == 'GB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{count} B'
        value /= 1024.0
    return f'{count} B'  # pragma: no cover — unreachable


class PresentationExportRunner(QObject):
    """Runs one review/proposal package export at a time on a worker lane.

    Mirrors ``SupportHealthRunner``'s contract: ``start`` returns False
    while busy or closed; ``request_cancel`` feeds the cooperative flag
    through the ActivityCenter record; ``shutdown`` cancels and drains
    the pool (workspace close / project switch) without ever destroying
    a still-running ``QThread``.
    """

    export_started = Signal(object)    # PresentationExportJob
    export_progress = Signal(object)   # PackageBuildProgress (relayed)
    export_completed = Signal(object)  # _BuiltPackage — published
    export_failed = Signal(object)     # (job, error_text, diagnostic_id)
    export_cancelled = Signal()
    export_finished = Signal()         # any terminal state — re-arm UI

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        presentation_repository: CadPresentationRepository,
        comparison_repository: CadDesignComparisonRepository,
        decision_repository: CadDesignDecisionRepository,
        activity_center: ActivityCenter | None = None,
        renderer_factory: Callable[
            [PresentationSession], object
        ] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._scene_repository = scene_repository
        self._presentation_repository = presentation_repository
        self._comparison_repository = comparison_repository
        self._decision_repository = decision_repository
        self._activity_center = activity_center
        self._renderer_factory = (
            renderer_factory
            if renderer_factory is not None
            else _default_renderer_factory
        )
        self._pool = NativeWorkerPool(self)
        self._job: PresentationExportJob | None = None
        self._operation_id: str | None = None
        self._task_key: str | None = None
        self._closed = False
        self.export_progress.connect(self._apply_progress)

    # -- lifecycle --------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._job is not None

    @property
    def current_job(self) -> PresentationExportJob | None:
        return self._job

    def start(self, job: PresentationExportJob) -> bool:
        """Queue ``job`` on the pool; False when busy/shut down.

        The ActivityCenter record is submitted and marked running before
        the worker lane starts, so the operation is visible to the
        operator for the job's whole life.
        """
        if self._closed or self._job is not None:
            return False
        task_key = f'presentation.export.{job.kind}.{uuid.uuid4().hex[:12]}'
        operation_id = None
        center = self._activity_center
        if center is not None:
            session = job.session
            operation_id = center.submit(
                operation_kind=f'presentation.export.{job.kind}',
                operation_class=OperationClass.EXTERNAL_IO,
                title=(
                    f'{_KIND_LABEL[job.kind]}を生成 — '
                    f'セッション「{session.label}」'
                ),
                document_ref=session.document_id,
                # project_ref scopes the row to 「このプロジェクト」 —
                # the ActivityPage matches it against document ids.
                project_ref=session.document_id,
                revision_ref=session.scene_revision_id,
                input_authority_refs=(
                    f'presentation-session:{session.session_sha256}',
                    'scene-revision:'
                    f'{session.scene_revision_id}:'
                    f'{session.scene_content_hash}',
                ),
                cancellability=Cancellability.CANCEL_UNTIL_COMMIT,
                retry_policy=RetryPolicy.SAFE_NEW_ATTEMPT,
                navigation_policy=NavigationPolicy.BACKGROUNDABLE,
                deep_link=WorkspaceDeepLink(
                    WorkspaceId.PRESENTATION,
                    'export',
                    revision_id=session.scene_revision_id,
                ),
                cancel_callback=lambda: self._pool.cancel(task_key),
            )
            center.mark_running(operation_id)
        self._job = job
        self._operation_id = operation_id
        self._task_key = task_key
        self._pool.start(
            task_key,
            self._make_operation(job),
            self._on_completed,
            on_finished=self._job_finished,
        )
        self.export_started.emit(job)
        return True

    def request_cancel(self) -> bool:
        """Ask the running job to stop at its next safe point."""
        op_id = self._operation_id
        if op_id is not None and self._activity_center is not None:
            return self._activity_center.request_cancel(op_id)
        if self._task_key is not None:
            return self._pool.cancel(self._task_key)
        return False

    def shutdown(self) -> None:
        """Cancel any in-flight export and drain the worker lane.

        Called from the workspace's ``close()`` (mount disposal on
        project switch) — bounded wait, detached workers still run
        their own staging cleanup before exiting.
        """
        if self._closed:
            return
        self._closed = True
        op_id = self._operation_id
        if op_id is not None and self._activity_center is not None:
            self._activity_center.request_cancel(op_id)
        else:
            self._pool.cancel_all()
        self._pool.shutdown()
        if op_id is not None and self._activity_center is not None:
            op = self._activity_center.get(op_id)
            if op is not None and op.is_active:
                try:
                    self._activity_center.confirm_cancelled(op_id)
                except EXPECTED_OPERATION_ERRORS:  # error-boundary: completion record — an expected transition race is benign (the op is already terminal); unexpected errors propagate
                    pass
        self._job = None
        self._operation_id = None
        self._task_key = None
        self.export_finished.emit()

    # -- worker lane --------------------------------------------------------

    def _make_operation(
        self, job: PresentationExportJob
    ) -> Callable[[object], _BuiltPackage]:
        def operation(cancel_event) -> _BuiltPackage:
            staging_dir = Path(
                tempfile.mkdtemp(
                    prefix=f'.{job.package_dir.name}.staging-',
                    dir=str(job.output_root),
                )
            )
            try:
                if job.kind == 'review':
                    # The renderer is created here, on the worker thread —
                    # VTK/Qt thread affinity is honored and no live
                    # QtInteractor is touched cross-thread (#985).
                    renderer = self._renderer_factory(job.session)
                    result: ReviewPackageResult | ProposalPackageResult = (
                        build_review_package(
                            job.session,
                            staging_dir,
                            self._scene_repository,
                            presentation_repository=(
                                self._presentation_repository
                            ),
                            renderer=renderer,
                            include_drawings=job.include_drawings,
                            yaw_steps_deg=job.yaw_steps_deg,
                            progress=self._relay_progress,
                            cancel_event=cancel_event,
                        )
                    )
                    verify_stage = _VERIFY_STAGE['review']
                    self._relay_progress(
                        PackageBuildProgress(
                            stage_label='検証',
                            stage_index=verify_stage,
                            stage_count=verify_stage,
                        )
                    )
                    verify_review_package(staging_dir)
                else:  # proposal
                    result = build_proposal_package(
                        job.session,
                        staging_dir,
                        self._scene_repository,
                        presentation_repository=(
                            self._presentation_repository
                        ),
                        comparison_repository=self._comparison_repository,
                        decision_repository=self._decision_repository,
                        progress=self._relay_progress,
                        cancel_event=cancel_event,
                    )
                    verify_stage = _VERIFY_STAGE['proposal']
                    self._relay_progress(
                        PackageBuildProgress(
                            stage_label='検証',
                            stage_index=verify_stage,
                            stage_count=verify_stage,
                        )
                    )
                    verify_proposal_package(staging_dir)
                # A cancel landing after the builder's last check but
                # before the lane signals completion must still remove
                # the staged tree — raising here routes through the
                # except block below instead of returning a publishable
                # result.
                if cancel_event.is_set():
                    raise ExportCancelledError()
            except BaseException:  # error-boundary: cleanup before re-raise — cancel/failure/crash all remove the staged tree so no partial package survives to look finished (noqa: BLE001)
                # Cancel / failure / renderer crash: remove the staged
                # tree so no partial package survives to look finished.
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise
            return _BuiltPackage(
                job=job, staging_dir=staging_dir, result=result
            )

        return operation

    def _relay_progress(self, progress: PackageBuildProgress) -> None:
        """Worker-thread progress → queued Signal → UI-thread registry."""
        self.export_progress.emit(progress)

    # -- UI-thread handlers -------------------------------------------------

    def _apply_progress(self, progress: PackageBuildProgress) -> None:
        op_id = self._operation_id
        if op_id is None or self._activity_center is None:
            return
        if progress.done_units is not None and progress.total_units:
            op_progress = OperationProgress(
                kind=ProgressKind.ITEMS,
                done_units=progress.done_units,
                total_units=progress.total_units,
                unit_label=progress.unit_label,
                stage_label=(
                    f'{progress.stage_label}'
                    f'（{progress.stage_index}/{progress.stage_count}）'
                ),
            )
        else:
            op_progress = OperationProgress(
                kind=ProgressKind.STAGE,
                stage_index=progress.stage_index,
                stage_count=progress.stage_count,
                stage_label=progress.stage_label,
            )
        try:
            self._activity_center.update_progress(op_id, op_progress)
        except Exception:  # error-boundary: best-effort progress — progress is reporting, not authority; a raced terminal transition must never break the job (noqa: BLE001)
            # Progress is reporting, not authority — a raced terminal
            # transition must never break the job.
            pass

    def _on_completed(
        self, key: object, result: object, error: object
    ) -> None:
        del key
        job = self._job
        center = self._activity_center
        op_id = self._operation_id

        if error == WORKER_CANCELLED:
            # The worker removed its staging tree on the exception
            # path (ExportCancelledError or an aborted build).
            self._mark_cancelled()
            return
        if error is not None or not isinstance(result, _BuiltPackage):
            self._mark_failed(job, error)
            return

        # A cancel request that arrived after the build+verify finished
        # is still honored — the verified staging tree is discarded and
        # nothing is published.
        if (
            center is not None
            and op_id is not None
            and (op := center.get(op_id)) is not None
            and op.state == OperationState.CANCELLATION_REQUESTED
        ):
            self._discard(result.staging_dir)
            self._mark_cancelled()
            return

        # The pinned job carried by the result is authoritative — the
        # runner's ``self._job`` may already be cleared/replaced.
        job = result.job
        # Commit point: cancel is no longer offered once publication
        # (one atomic rename) begins.
        if center is not None and op_id is not None:
            try:
                center.mark_commit_point(op_id)
            except Exception as exc:  # error-boundary: completion boundary — any commit-point failure discards the staged tree and marks the job failed with the exception identity (noqa: BLE001)
                self._discard(result.staging_dir)
                self._mark_failed(job, exc)
                return
        try:
            _publish_staged(result.staging_dir, job.package_dir)
        except Exception as exc:  # error-boundary: completion boundary — any publish failure discards the staged tree and marks the job failed with the exception identity (noqa: BLE001)
            self._discard(result.staging_dir)
            self._mark_failed(job, exc)
            return

        if center is not None and op_id is not None:
            try:
                center.complete(
                    op_id, result_summary=self._result_summary(job, result)
                )
            except Exception:  # error-boundary: activity record — a completion-record failure must not undo a published package (noqa: BLE001)
                pass
            try:
                stale = self._presentation_repository.session_stale(
                    job.session
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: staleness probe — expected failures report and degrade to 'not stale' honestly; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='セッション鮮度の確認')
                stale = False
            if stale:
                # The pinned input is no longer the document head — the
                # published package stays valid evidence for its pinned
                # revision but is reclassified as historical, never as a
                # current result.
                center.note_authorities_changed(
                    {
                        'presentation-session:'
                        f'{job.session.session_sha256}',
                        'scene-revision:'
                        f'{job.session.scene_revision_id}:'
                        f'{job.session.scene_content_hash}',
                    }
                )
        self.export_completed.emit(result)

    def _job_finished(self, key: str) -> None:
        del key
        self._job = None
        self._operation_id = None
        self._task_key = None
        self.export_finished.emit()

    # -- terminal handling helpers -----------------------------------------

    def _mark_cancelled(self) -> None:
        op_id = self._operation_id
        if op_id is not None and self._activity_center is not None:
            try:
                self._activity_center.confirm_cancelled(op_id)
            except EXPECTED_OPERATION_ERRORS:  # error-boundary: completion record — an expected transition race is benign (the op is already terminal); unexpected errors propagate
                pass
        self.export_cancelled.emit()

    def _mark_failed(
        self, job: PresentationExportJob | None, error: object
    ) -> None:
        diagnostic_id = failure_correlation_id()
        error_text = (
            operation_error_message(error)
            if isinstance(error, BaseException)
            else str(error or 'unknown failure')
        )
        op_id = self._operation_id
        if op_id is not None and self._activity_center is not None:
            try:
                self._activity_center.fail(
                    op_id,
                    error_summary=(
                        f'{error_text} [diag: {diagnostic_id}]'
                    ),
                    diagnostic_id=diagnostic_id,
                )
            except EXPECTED_OPERATION_ERRORS:  # error-boundary: failure record — an expected activity-record failure is benign (the export_failed signal still carries the identity); unexpected errors propagate
                pass
        logger.warning(
            'presentation export failed %s: %r', diagnostic_id, error
        )
        self.export_failed.emit((job, error_text, diagnostic_id))

    @staticmethod
    def _discard(staging_dir: Path) -> None:
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except OSError:
            pass

    @staticmethod
    def _result_summary(
        job: PresentationExportJob, built: _BuiltPackage
    ) -> str:
        manifest = built.result.manifest
        entries = tuple(manifest.entries)
        total_bytes = sum(entry.byte_length for entry in entries)
        warnings = len(built.result.warnings)
        if job.kind == 'review':
            renders = sum(
                1 for entry in entries if entry.kind == 'render'
            )
            sheets = sum(
                1 for entry in entries if entry.kind == 'drawing'
            )
            summary = (
                f'フレーム {renders}/{job.expected_frames} 処理・'
                f'図面 {sheets} 枚・{_bytes_label(total_bytes)}'
            )
        else:
            summary = (
                f'エントリ {len(entries)} 件・{_bytes_label(total_bytes)}'
            )
        if warnings:
            summary += f'・警告 {warnings} 件'
        return summary
