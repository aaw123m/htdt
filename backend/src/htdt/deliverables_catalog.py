"""Project Deliverables catalog (#900).

A project-scoped, presentation-layer inventory answering: *what can this
exact project generate, from which pinned authority, what is missing or
degraded, and through which existing command?*

This is NOT a new output authority: every entry routes to the existing
domain generators — ``installation.export_handoff``,
``analysis.export_bundle``, ``equipment.export_capture_catalog`` — so
deliverable discovery lives in one place instead of scattered across
global palette commands and bespoke modal chains.

Scope discipline (issue #900 §1): only project deliverables and
interoperability exports are catalogued. Disaster-recovery backup,
portable ``.htdtproject`` transfer, storage maintenance and diagnostics
are deliberately absent — they are Data Management concerns and must
never appear beside "export measurement CSV" as equivalent actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cad_measurement_repository import CadMeasurementRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .overview_readiness import OverviewReadinessService


DeliverableCategory = Literal[
    'engineering_analysis',
    'installation_field',
    'commissioning_verification',
    'interoperability',
]

DeliverableAvailability = Literal[
    'available',
    'available_degraded',
    'stale_review',
    'blocked',
    'not_applicable',
]


class DeliverableEntry(BaseModel):
    """One generatable output with its availability and pinned sources."""

    model_config = ConfigDict(frozen=True)

    deliverable_id: str = Field(min_length=1)
    category: DeliverableCategory
    title: str = Field(min_length=1)
    availability: DeliverableAvailability
    #: Human-readable reason whenever not plainly available (missing input,
    #: degraded section, not-applicable scope).
    reason: str | None = None
    #: Exact authority pins the output would consume
    #: (e.g. ``scene_revision:<id>``, ``system_variant:<id>``).
    source_authorities: tuple[str, ...] = ()
    expected_formats: tuple[str, ...] = ()
    #: Existing command that performs generation — the catalog routes to
    #: real generators, never re-implements them.
    command_id: str | None = None
    #: Workspace deep-link for acquiring the missing input.
    action: WorkspaceDeepLink | None = None


class DeliverablesCatalogService:
    """Derives the project-scoped deliverable inventory from live authority."""

    def __init__(
        self,
        scene_repository,
        document_id: str,
        *,
        overview_service: 'OverviewReadinessService | None' = None,
    ) -> None:
        self._repository = scene_repository
        self._document_id = document_id
        self._overview = overview_service

    def catalog(self) -> tuple[DeliverableEntry, ...]:
        """Current deliverable inventory, ordered by taxonomy."""

        revisions = self._repository.list_revision_summaries(
            self._document_id
        )
        latest_revision_id = (
            revisions[-1].revision_id if revisions else None
        )
        variants = CadSystemVariantRepository(
            self._repository
        ).list_variants(self._document_id)
        latest_variant_id = (
            variants[-1].variant_id if variants else None
        )
        source_pins: tuple[str, ...] = tuple(
            pin
            for pin in (
                (
                    f'scene_revision:{latest_revision_id}'
                    if latest_revision_id is not None
                    else None
                ),
                (
                    f'system_variant:{latest_variant_id}'
                    if latest_variant_id is not None
                    else None
                ),
            )
            if pin is not None
        )

        revision = (
            self._repository.get(latest_revision_id)
            if latest_revision_id is not None
            else None
        )
        entities = () if revision is None else revision.document.entities
        room_present = revision is not None and revision.document.room is not None
        install_entities = any(
            entity.kind in ('speaker', 'av_equipment')
            and entity.kind != 'measurement_point'
            for entity in entities
        )
        role_defined_speakers = any(
            entity.kind == 'speaker' for entity in entities
        )

        measurements = CadMeasurementRepository(self._repository)
        measurement_count = len(
            measurements.list_measurements(self._document_id)
        ) + len(measurements.list_comparisons(self._document_id))

        view = (
            self._overview.read(self._document_id)
            if self._overview is not None
            else None
        )
        blocker_codes = (
            frozenset() if view is None else {b.code for b in view.blockers}
        )
        warning_codes = (
            frozenset() if view is None else {w.code for w in view.warnings}
        )
        install_blockers = (
            'room.missing' in blocker_codes
            or 'room.geometry_incomplete' in blocker_codes
            or not room_present
        )
        speaker_blockers = (
            'speaker.missing' in blocker_codes
            or 'speaker.role_missing' in blocker_codes
            or 'speaker.role_duplicate' in blocker_codes
        )
        install_reason: str | None = None
        if latest_revision_id is None:
            install_reason = '書き出せるシーンリビジョンがありません。'
        elif install_blockers:
            install_reason = '部屋権威が未確定です。'
        elif speaker_blockers:
            install_reason = 'スピーカー構成が未確定です。'

        def _install_entry(
            deliverable_id: str,
            title: str,
            *,
            formats: tuple[str, ...],
            action_section: str | None = None,
        ) -> DeliverableEntry:
            availability: DeliverableAvailability
            reason = install_reason
            if reason is not None:
                availability = 'blocked'
            elif warning_codes:
                # Real overview warnings (e.g. missing/unverified
                # measurements) render as incomplete/UNKNOWN sections in
                # the handoff — honest degradation, not a blocker (#900-3).
                availability = 'available_degraded'
                reason = '一部のセクションが未確定です（警告あり）。'
            else:
                availability = 'available'
            return DeliverableEntry(
                deliverable_id=deliverable_id,
                category='installation_field',
                title=title,
                availability=availability,
                reason=reason,
                source_authorities=source_pins,
                expected_formats=formats,
                command_id='installation.export_handoff',
                action=(
                    WorkspaceDeepLink(WorkspaceId.ROOM, action_section)
                    if install_reason is not None
                    else None
                ),
            )

        installation_handoff = _install_entry(
            'installation.handoff',
            '設置ハンドオフパッケージ',
            formats=('csv', 'md', 'json'),
            action_section='geometry',
        )
        drawing_set = _install_entry(
            'installation.drawing_set',
            '設置図面セット',
            formats=('svg', 'pdf'),
            action_section='geometry',
        )

        bom_reason: str | None = None
        if install_reason is not None:
            bom_reason = install_reason
        elif not install_entities and not role_defined_speakers:
            bom_reason = '設計権威に機材・スピーカーがありません。'
        bom = DeliverableEntry(
            deliverable_id='installation.bom',
            category='installation_field',
            title='機材BOM',
            availability='blocked' if bom_reason else 'available',
            reason=bom_reason,
            source_authorities=source_pins,
            expected_formats=('csv', 'json'),
            command_id='installation.export_handoff',
            action=(
                WorkspaceDeepLink(WorkspaceId.ROOM, 'placement')
                if bom_reason is not None
                else None
            ),
        )

        labels_reason: str | None = None
        if install_reason is not None:
            labels_reason = install_reason
        elif not install_entities:
            labels_reason = 'ラベル対象の設置機材・ケーブル権威がありません。'
        field_labels = DeliverableEntry(
            deliverable_id='field.labels',
            category='installation_field',
            title='現場ラベル',
            availability='blocked' if labels_reason else 'available',
            reason=labels_reason,
            source_authorities=source_pins,
            expected_formats=('csv', 'pdf'),
            command_id='installation.export_handoff',
            action=(
                WorkspaceDeepLink(WorkspaceId.ROOM, 'placement')
                if labels_reason is not None
                else None
            ),
        )

        analysis_reason: str | None = None
        if measurement_count == 0:
            analysis_reason = '書き出せる測定・比較データがありません。'
        analysis_bundle = DeliverableEntry(
            deliverable_id='analysis.bundle',
            category='engineering_analysis',
            title='解析エクスポート',
            availability='blocked' if analysis_reason else 'available',
            reason=analysis_reason,
            source_authorities=(
                source_pins + (f'project_data:{measurement_count}',)
                if measurement_count
                else source_pins
            ),
            expected_formats=('csv', 'json', 'html'),
            command_id='analysis.export_bundle',
            action=(
                WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'import')
                if analysis_reason is not None
                else None
            ),
        )

        commissioning_plans = self._commissioning_plan_count()
        commissioning_report = DeliverableEntry(
            deliverable_id='commissioning.report',
            category='commissioning_verification',
            title='コミッショニングレポート',
            availability=(
                'available' if commissioning_plans else 'not_applicable'
            ),
            reason=(
                None
                if commissioning_plans
                else 'コミッショニング権威がまだありません。'
            ),
            source_authorities=source_pins,
            expected_formats=('md', 'html'),
            action=WorkspaceDeepLink(WorkspaceId.OVERVIEW),
        )

        # Application-global interoperability export: scope is honest —
        # this entry is not bound to a document-scoped pin.
        capture_catalog = DeliverableEntry(
            deliverable_id='equipment.capture_catalog',
            category='interoperability',
            title='Capture用機材カタログ',
            availability='available',
            reason='アプリケーション共通の書き出し（プロジェクト非依存）。',
            source_authorities=('application_scope',),
            expected_formats=('csv', 'json'),
            command_id='equipment.export_capture_catalog',
        )

        return (
            analysis_bundle,
            installation_handoff,
            drawing_set,
            bom,
            field_labels,
            commissioning_report,
            capture_catalog,
        )

    def _commissioning_plan_count(self) -> int:
        try:
            from .cad_commissioning_repository import (
                CadCommissioningRepository,
            )

            return len(
                CadCommissioningRepository(self._repository).list_plans(
                    self._document_id
                )
            )
        except Exception:
            return 0


__all__ = [
    'DeliverableAvailability',
    'DeliverableCategory',
    'DeliverableEntry',
    'DeliverablesCatalogService',
]
