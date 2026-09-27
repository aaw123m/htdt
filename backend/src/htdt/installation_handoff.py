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

The published directory is a *package*: a ``handoff_manifest.json`` binds
every member file (name, size, SHA-256) to one generation — the exact
SceneRevision/SystemVariant/InstallationOutput semantic hash, the package
schema/generator version, the export timestamp and the machine-readable
complete/degraded review state. Semantic content is deterministic for the
same pinned authority; ``generated_at_utc`` is intentionally volatile
export metadata inside the HTML report and the manifest, so files are
byte-identical only across exports sharing that stamp.
"""

from __future__ import annotations

import csv
from hashlib import sha256
import io
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .csv_export import csv_safe_row
from .installation_output_authority import InstallationReportService
from .report import (
    InstallationOutput,
    InstallationSectionStatus,
    render_installation_csv,
    render_installation_report_html,
)
from .canonical_json import canonical_json as _canonical_json


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


class HandoffManifestFile(BaseModel):
    """Digest of one member file inside a published package."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)


class HandoffPackageManifest(BaseModel):
    """The machine-readable identity of one handoff package generation.

    Ties every member file to the exact authority it was exported from:
    an installer can verify all files came from the same generation and
    read the degraded section list without inferring completeness from
    file presence.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    generator: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    system_variant_id: str | None = None
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    complete: bool
    degraded: tuple[str, ...] = ()
    files: tuple[HandoffManifestFile, ...]


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
HANDOFF_MANIFEST_FILENAME = 'handoff_manifest.json'
HANDOFF_PACKAGE_GENERATOR = 'installation-handoff-1'




def build_handoff_manifest(
    handoff: InstallationHandoff,
    *,
    files: dict[str, bytes],
) -> HandoffPackageManifest:
    """The manifest sealing one package generation.

    ``files`` maps member filename to its exact bytes — the manifest
    records each digest so a reader can prove every member belongs to the
    same generation and read the review verdict without guessing from
    file presence.
    """

    return HandoffPackageManifest(
        generator=HANDOFF_PACKAGE_GENERATOR,
        generated_at_utc=handoff.generated_at_utc,
        scene_revision_id=handoff.output.authority.scene_revision_id,
        system_variant_id=handoff.output.authority.system_variant_id,
        semantic_sha256=handoff.output.semantic_sha256,
        complete=handoff.review.complete,
        degraded=handoff.review.degraded,
        files=tuple(
            HandoffManifestFile(
                name=name,
                sha256=sha256(content).hexdigest(),
                size_bytes=len(content),
            )
            for name, content in sorted(files.items())
        ),
    )


def render_handoff_manifest_json(
    handoff: InstallationHandoff,
    *,
    files: dict[str, bytes],
) -> str:
    """Canonical JSON for the package manifest."""

    manifest = build_handoff_manifest(handoff, files=files)
    return _canonical_json(manifest.model_dump(mode='json')) + '\n'


def _write_staged_text(path: Path, content: str) -> None:
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_handoff_package(
    handoff: InstallationHandoff, directory: str | Path
) -> dict[str, Path]:
    """Write the handoff package into ``directory`` and return its paths.

    The whole generation — the four member files plus the manifest that
    seals them — is rendered and fsync'd inside a ``.htdt-handoff-*``
    staging directory in the target, verified against the manifest
    digests, then promoted. Members are promoted first and the manifest
    last, so its digests always describe a complete on-disk generation.

    A failure anywhere *before or during* promotion restores the previous
    complete package: each replaced file's prior bytes are kept inside
    the staging directory and moved back on rollback, while files that
    did not exist before are removed. The target therefore always holds
    either the previous intact generation or the new one — never an
    unmarked mix of two.

    Determinism contract: semantic content is byte-deterministic for the
    same pinned authority and the same declared ``generated_at_utc`` —
    the HTML report and the manifest intentionally carry that volatile
    export stamp, so bytes are identical across regenerations sharing
    it, and the ``semantic_sha256`` inside never depends on the clock.
    """

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    member_filenames = {
        'report': HANDOFF_REPORT_FILENAME,
        'dimensions': HANDOFF_DIMENSIONS_FILENAME,
        'settings': HANDOFF_SETTINGS_FILENAME,
        'entities': HANDOFF_ENTITIES_FILENAME,
    }
    member_contents = {
        'report': render_handoff_report_html(handoff),
        'dimensions': render_dimension_sheets_csv(handoff),
        'settings': render_settings_csv(handoff),
        'entities': render_installation_csv(handoff.output),
    }
    manifest_content = render_handoff_manifest_json(
        handoff,
        files={
            member_filenames[key]: content.encode('utf-8')
            for key, content in member_contents.items()
        },
    )
    contents = {
        **member_contents,
        'manifest': manifest_content,
    }
    filenames = {
        **member_filenames,
        'manifest': HANDOFF_MANIFEST_FILENAME,
    }
    staging = Path(
        tempfile.mkdtemp(prefix='.htdt-handoff-', dir=target)
    )
    expected_digests = {
        key: sha256(contents[key].encode('utf-8')).hexdigest()
        for key in contents
    }
    try:
        staged = {
            key: staging / name for key, name in filenames.items()
        }
        for key, path in staged.items():
            _write_staged_text(path, contents[key])
            if (
                sha256(path.read_bytes()).hexdigest()
                != expected_digests[key]
            ):
                raise IOError(
                    f'staged handoff file {path.name} failed digest '
                    'verification before publish'
                )
        backups: dict[str, Path] = {}
        promoted: list[str] = []
        # Members first, manifest last — the manifest's digests must only
        # ever describe a fully written generation.
        promote_order = [*member_filenames, 'manifest']
        try:
            for key in promote_order:
                final = target / filenames[key]
                if final.exists():
                    backup = staging / '.backup' / filenames[key]
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(final, backup)
                    backups[key] = backup
                os.replace(staged[key], final)
                promoted.append(key)
        except Exception:
            # Roll back to the previous complete generation: restore every
            # file moved aside into backup (promoted or merely displaced by
            # a later failure) and drop members that had no predecessor.
            for key in reversed(promote_order):
                final = target / filenames[key]
                backup = backups.get(key)
                if backup is not None:
                    os.replace(backup, final)
                elif key in promoted:
                    final.unlink(missing_ok=True)
            raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return {key: target / name for key, name in filenames.items()}


__all__ = [
    'HANDOFF_DIMENSIONS_FILENAME',
    'HANDOFF_ENTITIES_FILENAME',
    'HANDOFF_MANIFEST_FILENAME',
    'HANDOFF_PACKAGE_GENERATOR',
    'HANDOFF_REPORT_FILENAME',
    'HANDOFF_SETTINGS_FILENAME',
    'HandoffManifestFile',
    'HandoffPackageManifest',
    'HandoffReview',
    'InstallationHandoff',
    'build_handoff_manifest',
    'build_installation_handoff',
    'handoff_preview_text',
    'render_dimension_sheets_csv',
    'render_handoff_manifest_json',
    'render_handoff_report_html',
    'render_settings_csv',
    'review_installation_output',
    'write_handoff_package',
]
