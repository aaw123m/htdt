"""Native installation handoff (#453).

The installer-facing handoff exposes dimension sheets, settings and the
project report from *exact stored authority*: the caller selects a
SceneRevision (and optionally a SystemVariant) explicitly, the service
materializes :class:`InstallationOutput` read-only, and every exported file
is byte-deterministic for the same pinned authority. Nothing in this module
mutates project state — export is a pure read.

Before export, the handoff is *reviewed*: every
:class:`InstallationSectionStatus` that is not ``AVAILABLE`` is surfaced in
the review so the operator sees exactly what the package is missing instead
of discovering it on site.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .csv_export import csv_safe_row
from .installation_output_authority import InstallationReportService
from .report import (
    InstallationOutput,
    InstallationSectionStatus,
    render_installation_csv,
    render_installation_report_html,
)


class HandoffReview(BaseModel):
    """Completeness verdict over an installation output's sections."""

    model_config = ConfigDict(frozen=True)

    complete: bool
    sections: tuple[InstallationSectionStatus, ...]
    degraded: tuple[str, ...]


class InstallationHandoff(BaseModel):
    """The reviewed installation output ready for export."""

    model_config = ConfigDict(frozen=True)

    output: InstallationOutput
    review: HandoffReview
    generated_at_utc: str = Field(min_length=1)


def review_installation_output(output: InstallationOutput) -> HandoffReview:
    """Surface every non-AVAILABLE section before export."""

    degraded = tuple(
        item.section for item in output.sections if item.status != 'AVAILABLE'
    )
    return HandoffReview(
        complete=not degraded,
        sections=output.sections,
        degraded=degraded,
    )


def build_installation_handoff(
    service: InstallationReportService,
    *,
    scene_revision_id: str,
    generated_at_utc: str,
    system_variant_id: str,
) -> InstallationHandoff:
    """Materialize an installation handoff from explicit authority ids.

    ``scene_revision_id`` is required and ``system_variant_id`` is an
    explicit selection (no implicit "current variant" fallback): the
    handoff only ever packages exactly what was chosen. The underlying
    service resolves every bound authority fail-closed.
    """

    output = service.build_installation_output_from_authorities(
        scene_revision_id=scene_revision_id,
        system_variant_id=system_variant_id or None,
    )
    return InstallationHandoff(
        output=output,
        review=review_installation_output(output),
        generated_at_utc=generated_at_utc,
    )


def _csv_number(value: float | None) -> str:
    return '' if value is None else format(float(value), '.12g')


def render_dimension_sheets_csv(handoff: InstallationHandoff) -> str:
    """Byte-deterministic dimension sheet export (top/front/side views)."""

    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(
        csv_safe_row(
            (
                'view',
                'horizontal_axis',
                'vertical_axis',
                'horizontal_min_m',
                'horizontal_max_m',
                'vertical_min_m',
                'vertical_max_m',
                'entity_id',
                'entity_kind',
                'horizontal_m',
                'vertical_m',
                'scene_revision_id',
                'system_variant_id',
                'semantic_sha256',
            )
        )
    )
    output = handoff.output
    for sheet in output.dimensions:
        for point in sheet.points:
            writer.writerow(
                csv_safe_row(
                    (
                        sheet.view,
                        sheet.horizontal_axis,
                        sheet.vertical_axis,
                        _csv_number(sheet.horizontal_min_m),
                        _csv_number(sheet.horizontal_max_m),
                        _csv_number(sheet.vertical_min_m),
                        _csv_number(sheet.vertical_max_m),
                        point.entity_id,
                        point.entity_kind,
                        _csv_number(point.horizontal_m),
                        _csv_number(point.vertical_m),
                        output.authority.scene_revision_id,
                        output.authority.system_variant_id or '',
                        output.semantic_sha256,
                    )
                )
            )
    return stream.getvalue()


def render_settings_csv(handoff: InstallationHandoff) -> str:
    """Byte-deterministic settings export.

    Settings are the calibration channel settings carried on the bound
    authority; when the calibration section is not ``AVAILABLE`` the export
    still renders deterministically with an explicit empty body rather than
    omitting the section silently.
    """

    output = handoff.output
    calibration = output.calibration
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(
        csv_safe_row(
            (
                'settings_source',
                'channel_id',
                'physical_output_id',
                'gain_db',
                'delay_s',
                'polarity',
                'lifecycle_state',
                'scene_revision_id',
                'system_variant_id',
                'semantic_sha256',
            )
        )
    )
    if calibration is not None:
        for item in (
            *calibration.requested_channels,
            *calibration.exported_channels,
        ):
            writer.writerow(
                csv_safe_row(
                    (
                        item.settings_source,
                        item.channel_id,
                        item.physical_output_id,
                        _csv_number(item.gain_db),
                        _csv_number(item.delay_s),
                        item.polarity,
                        calibration.lifecycle_state or '',
                        output.authority.scene_revision_id,
                        output.authority.system_variant_id or '',
                        output.semantic_sha256,
                    )
                )
            )
    return stream.getvalue()


def render_handoff_report_html(handoff: InstallationHandoff) -> str:
    """The installation report as a self-contained HTML document."""

    return render_installation_report_html(
        handoff.output, exported_at_utc=handoff.generated_at_utc
    )


def _preview_number(value: float | None) -> str:
    return '—' if value is None else format(float(value), '.4g')


def handoff_preview_text(handoff: InstallationHandoff) -> str:
    """A readable preview of exactly what the package will contain.

    This is the operator's last look before writing: exact authority pins,
    entity coordinates, per-view dimension sheets, equipment/treatment
    summary, calibration settings, standards evidence and every degraded
    section — never just record counts.
    """

    output = handoff.output
    lines = [
        f'シーンリビジョン: {output.authority.scene_revision_id}',
        f'システムバリアント: {output.authority.system_variant_id or "（なし）"}',
        f'セマンティックSHA: {output.semantic_sha256}',
        '',
        '== エンティティ座標 (m) ==',
    ]
    if output.entities:
        for entity in output.entities:
            role = f' role={entity.speaker_role}' if entity.speaker_role else ''
            lines.append(
                f'  {entity.name} [{entity.entity_kind}]{role}: '
                f'x={entity.x_m:.3f} y={entity.y_m:.3f} z={entity.z_m:.3f} '
                f'yaw={entity.body_yaw_deg:.1f}°'
            )
    else:
        lines.append('  （エンティティなし）')
    lines.append('')
    lines.append('== 寸法図 ==')
    if output.dimensions:
        for sheet in output.dimensions:
            lines.append(
                f'  {sheet.view}: '
                f'{sheet.horizontal_axis} '
                f'{_preview_number(sheet.horizontal_min_m)}–'
                f'{_preview_number(sheet.horizontal_max_m)} m × '
                f'{sheet.vertical_axis} '
                f'{_preview_number(sheet.vertical_min_m)}–'
                f'{_preview_number(sheet.vertical_max_m)} m '
                f'({len(sheet.points)} 点)'
            )
    else:
        lines.append('  （寸法図なし）')
    lines.append('')
    lines.append('== 機材・トリートメント ==')
    treatment = output.treatment
    if treatment is not None and treatment.status == 'AVAILABLE':
        for quantity in treatment.quantities:
            lines.append(
                f'  {quantity.definition_id} '
                f'v{quantity.definition_version} [{quantity.lifecycle}]: '
                f'x{quantity.quantity} / '
                f'{quantity.total_face_area_m2:.2f} m²'
            )
        if not treatment.quantities:
            lines.append('  （トリートメントなし）')
    else:
        lines.append('  トリートメント情報: 未解決')
    lines.append('')
    lines.append('== 校正設定 ==')
    calibration = output.calibration
    if calibration is not None and calibration.status == 'AVAILABLE':
        for item in (
            *calibration.requested_channels,
            *calibration.exported_channels,
        ):
            lines.append(
                f'  {item.settings_source} {item.channel_id}: '
                f'gain={_preview_number(item.gain_db)} dB '
                f'delay={_preview_number(item.delay_s)} s '
                f'polarity={item.polarity} → {item.physical_output_id}'
            )
        if not calibration.requested_channels and not calibration.exported_channels:
            lines.append('  （チャンネル設定なし）')
    else:
        lines.append('  校正情報: 未解決')
    lines.append('')
    lines.append('== 規格証跡 ==')
    standards = output.standards
    if standards is not None and standards.status == 'AVAILABLE':
        lines.append(
            f'  プロファイル: {standards.profile_id} '
            f'v{standards.profile_version}'
        )
        lines.append(f'  評価: {standards.evaluation_id}')
        for criterion in standards.criteria:
            lines.append(
                f'  - {criterion.criterion_id}: {criterion.status} '
                f'({criterion.reason_code})'
            )
        if not standards.criteria:
            lines.append('  （評価基準なし）')
    else:
        lines.append('  規格情報: 未解決')
    lines.append('')
    lines.append('== セクション ==')
    for section in output.sections:
        lines.append(
            f'  {section.section}: {section.status} — {section.reason}'
        )
    if handoff.review.complete:
        lines.append('')
        lines.append('すべてのセクションが AVAILABLE です。')
    else:
        lines.append('')
        lines.append(
            '未解決のセクションがあります: '
            + ', '.join(handoff.review.degraded)
        )
    return '\n'.join(lines)


HANDOFF_DIMENSIONS_FILENAME = 'dimension_sheets.csv'
HANDOFF_SETTINGS_FILENAME = 'settings.csv'
HANDOFF_REPORT_FILENAME = 'installation_report.html'
HANDOFF_ENTITIES_FILENAME = 'installation_coordinates.csv'


def write_handoff_package(
    handoff: InstallationHandoff, directory: str | Path
) -> dict[str, Path]:
    """Write the handoff files into ``directory`` and return their paths.

    Publish is staged and atomic per file: every artifact is rendered into
    a temporary staging directory *inside* the target, then moved into
    place with :func:`os.replace`. A failure before publishing removes the
    staging directory and leaves the target exactly as it was; a failure
    mid-publish leaves only already-complete files (no half-written
    artifact is ever visible under a final name).

    Byte-determinism: the CSV bodies are byte-deterministic for the same
    pinned authority; the HTML report additionally embeds the export's
    ``generated_at_utc`` wall-clock stamp, so it is byte-identical across
    regenerations sharing that stamp and the same authority — the pinned
    ``semantic_sha256`` inside it never depends on the clock.
    """

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    filenames = {
        'report': HANDOFF_REPORT_FILENAME,
        'dimensions': HANDOFF_DIMENSIONS_FILENAME,
        'settings': HANDOFF_SETTINGS_FILENAME,
        'entities': HANDOFF_ENTITIES_FILENAME,
    }
    contents = {
        'report': render_handoff_report_html(handoff),
        'dimensions': render_dimension_sheets_csv(handoff),
        'settings': render_settings_csv(handoff),
        'entities': render_installation_csv(handoff.output),
    }
    staging = Path(
        tempfile.mkdtemp(prefix='.htdt-handoff-', dir=target)
    )
    try:
        staged = {
            key: staging / name for key, name in filenames.items()
        }
        for key, path in staged.items():
            path.write_text(contents[key], encoding='utf-8')
        outputs = {
            key: target / name for key, name in filenames.items()
        }
        for key, staged_path in staged.items():
            os.replace(staged_path, outputs[key])
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return outputs


__all__ = [
    'HANDOFF_DIMENSIONS_FILENAME',
    'HANDOFF_ENTITIES_FILENAME',
    'HANDOFF_REPORT_FILENAME',
    'HANDOFF_SETTINGS_FILENAME',
    'HandoffReview',
    'InstallationHandoff',
    'build_installation_handoff',
    'handoff_preview_text',
    'render_dimension_sheets_csv',
    'render_handoff_report_html',
    'render_settings_csv',
    'review_installation_output',
    'write_handoff_package',
]
