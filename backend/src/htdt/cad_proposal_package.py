"""Proposal-ready package — PM40 (#534).

Assembles the client-facing document a review ends with: an offline
``proposal.html`` (cover, derived drawing sheets, option comparison,
key values, open questions, decision records, provenance) plus the same
content-addressed ``manifest.json`` convention the review package uses.

Every number comes from ``build_installation_output`` over the session's
pinned authority — nothing is re-measured or re-stated downstream. When
a section cannot be filled from authority the document says so in a
dedicated 未確定事項 (open items) block instead of omitting it.
"""

from __future__ import annotations

import html
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_design_comparison import (
    ComparisonAlternative,
    DesignComparisonSet,
    evaluate_comparison_set,
)
from .cad_design_comparison_repository import CadDesignComparisonRepository
from .cad_design_decision_repository import CadDesignDecisionRepository
from .cad_drawing_set import (
    build_drawing_set_spec,
    generate_drawing_set,
)
from .cad_presentation_repository import CadPresentationRepository
from .cad_presentation_session import PresentationSession
from .cad_repository import SceneRepository
from .canonical_json import canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now
from .report import InstallationOutput, build_installation_output


PROPOSAL_PACKAGE_SCHEMA = 'htdt.presentation-proposal-package'
PROPOSAL_PACKAGE_SCHEMA_VERSION = 1
PROPOSAL_PACKAGE_KIND = 'proposal'

PACKAGE_GENERATOR = 'htdt.presentation-proposal-package'


class ProposalPackageError(ValueError):
    """The proposal package cannot be built or verified honestly."""


class ProposalPackageEntry(BaseModel):
    """One file in the package — relative path + content hash + role."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_length: int = Field(ge=0)
    kind: Literal['document', 'manifest', 'semantic']

    @model_validator(mode='after')
    def valid_entry(self) -> 'ProposalPackageEntry':
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or '..' in path.parts
            or '\\' in self.path
            or ':' in self.path
        ):
            raise ValueError(
                'package entry path must be a relative POSIX path: '
                f'{self.path!r}'
            )
        return self


class ProposalPackageManifest(BaseModel):
    """Content-addressed manifest over one built proposal package."""

    model_config = ConfigDict(frozen=True)

    schema: Literal['htdt.presentation-proposal-package'] = (
        PROPOSAL_PACKAGE_SCHEMA
    )
    schema_version: Literal[1] = PROPOSAL_PACKAGE_SCHEMA_VERSION
    package_kind: Literal['proposal'] = PROPOSAL_PACKAGE_KIND
    package_id: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    generator: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    session_label: str = Field(min_length=1)
    status_label: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    installation_output_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    comparison_set_id: str | None = Field(default=None, min_length=1)
    comparison_set_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    entries: tuple[ProposalPackageEntry, ...]
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_manifest(self) -> 'ProposalPackageManifest':
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError('package entry paths must be unique')
        if self.manifest_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProposalPackageManifest hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'package_kind': self.package_kind,
            'package_id': self.package_id,
            'generated_at_utc': self.generated_at_utc,
            'generator': self.generator,
            'session_id': self.session_id,
            'session_sha256': self.session_sha256,
            'session_label': self.session_label,
            'status_label': self.status_label,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'installation_output_sha256': self.installation_output_sha256,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'comparison_set_id': self.comparison_set_id,
            'comparison_set_sha256': self.comparison_set_sha256,
            'entries': [
                entry.model_dump(mode='json') for entry in self.entries
            ],
        }


class ProposalPackageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    output_dir: str
    manifest: ProposalPackageManifest
    warnings: tuple[str, ...] = ()


_STATUS_BADGES = {
    'draft': '下書き',
    'proposed': '提案（未確定）',
    'accepted': '承認済み案',
    'as_built': '竣工実績',
    'superseded': '旧版',
}

_SECTION_STATUS_JA = {
    'AVAILABLE': '利用可能',
    'UNKNOWN': '不明',
    'OMITTED': '対象外',
}

_AVAILABILITY_JA = {
    'available': '利用可能',
    'missing_reference': '参照先なし',
    'foreign_project': '他プロジェクト',
    'semantic_hash_conflict': 'ハッシュ不一致',
    'incompatible_baseline': '基準不一致',
    'context_mismatch': '文脈不一致',
    'unresolvable': '解決不能',
    'unsupported': '非対応',
}


def _proposal_html(
    session: PresentationSession,
    output: InstallationOutput,
    drawings_svg: tuple[tuple[str, str], ...],
    comparison_set: DesignComparisonSet | None,
    availability_rows: tuple[tuple[str, str, str, str], ...],
    decisions: tuple,
    generated_at_utc: str,
    warnings: tuple[str, ...],
) -> str:
    e = html.escape
    status_text = _STATUS_BADGES.get(session.status_label, session.status_label)
    authority = output.authority

    entity_rows = ''.join(
        '<tr>'
        f'<td>{e(item.name)}</td>'
        f'<td><code>{e(item.entity_id)}</code></td>'
        f'<td>{e(item.entity_kind)}</td>'
        f'<td>{e(item.speaker_role or "—")}</td>'
        f'<td>{item.x_m:.3f}</td><td>{item.y_m:.3f}</td>'
        f'<td>{item.z_m:.3f}</td>'
        f'<td>{item.body_yaw_deg:.1f}°</td>'
        f'<td>{e(item.collision_geometry_authority or "—")}</td>'
        '</tr>'
        for item in output.entities
    ) or '<tr><td colspan="9">対象エンティティはありません。</td></tr>'

    section_rows = ''.join(
        '<tr>'
        f'<td>{e(item.section)}</td>'
        f'<td>{e(_SECTION_STATUS_JA.get(item.status, item.status))}</td>'
        f'<td>{e(item.reason)}</td>'
        '</tr>'
        for item in output.sections
    )

    drawing_blocks = ''.join(
        f'<figure class="sheet"><figcaption>{e(title)}</figcaption>{svg}</figure>'
        for title, svg in drawings_svg
    )

    comparison_block = ''
    if comparison_set is not None:
        alternative_rows = ''.join(
            '<tr>'
            f'<td>{e(item.label)}</td>'
            f'<td><code>{e(item.scene_revision_id[:12])}…</code></td>'
            f'<td><code>{e(item.scene_content_hash[:12])}…</code></td>'
            f'<td>{e(item.system_variant_id or "—")}</td>'
            f'<td>{e(item.semantic_change_summary or "—")}</td>'
            '</tr>'
            for item in comparison_set.alternatives
        )
        availability_table = ''
        if availability_rows:
            availability_table = (
                '<h4>エビデンス利用可否</h4><table><thead><tr>'
                '<th>案</th><th>種別</th><th>状態</th><th>理由</th>'
                '</tr></thead><tbody>'
                + ''.join(
                    f'<tr><td>{e(a)}</td><td>{e(k)}</td>'
                    f'<td>{e(_AVAILABILITY_JA.get(s, s))}</td><td>{e(r)}</td></tr>'
                    for a, k, s, r in availability_rows
                )
                + '</tbody></table>'
            )
        comparison_block = f"""
<section id="comparison">
  <h3>比較案（{e(comparison_set.name)}）</h3>
  <table><thead><tr><th>案</th><th>シーンリビジョン</th><th>内容ハッシュ</th>
  <th>バリアント</th><th>変更要約</th></tr></thead>
  <tbody>{alternative_rows}</tbody></table>
  {availability_table}
</section>"""

    decision_rows = ''.join(
        '<tr>'
        f'<td>{e(item.title)}</td>'
        f'<td>{e(item.decision_scope)}</td>'
        f'<td>{e(item.selected_ref.kind)}:{e(item.selected_ref.ref_id[:16])}…</td>'
        f'<td>{e(item.rationale_note or "—")}</td>'
        f'<td>{e(item.author or "—")}</td>'
        f'<td>{e(item.created_at_utc)}</td>'
        '</tr>'
        for item in decisions
    )
    decisions_block = (
        '<section id="decisions"><h3>決定記録</h3>'
        '<table><thead><tr><th>件名</th><th>範囲</th><th>選択</th>'
        '<th>根拠</th><th>記録者</th><th>日時</th></tr></thead>'
        f'<tbody>{decision_rows or "<tr><td colspan=6>記録なし</td></tr>"}'
        '</tbody></table></section>'
    )

    open_items: list[str] = []
    for item in output.sections:
        if item.status != 'AVAILABLE':
            open_items.append(
                f'{item.section}: '
                f'{_SECTION_STATUS_JA.get(item.status, item.status)} — '
                f'{item.reason}'
            )
    for item in decisions:
        for limitation in item.evidence_limitations:
            open_items.append(
                f'決定「{item.title}」時点の制約: {limitation}'
            )
    for warning in warnings:
        open_items.append(warning)
    open_block = ''.join(f'<li>{e(item)}</li>' for item in open_items)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{e(session.label)} — HTDT 提案書</title>
<style>
body {{ margin:0; background:#f7f7f4; color:#1c2430; font:14px/1.6 "Segoe UI","Yu Gothic",system-ui,sans-serif; }}
.page {{ max-width:960px; margin:0 auto; padding:40px 32px 64px; background:#fff; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
h2 {{ font-size:18px; margin:32px 0 8px; border-bottom:2px solid #23405f; padding-bottom:4px; }}
h3 {{ font-size:15px; margin:24px 0 8px; }}
h4 {{ font-size:13px; margin:16px 0 4px; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th, td {{ border:1px solid #d4d9e0; padding:5px 8px; text-align:left; vertical-align:top; }}
th {{ background:#eef2f6; }}
code {{ font-family:ui-monospace,Consolas,monospace; font-size:11px; }}
.badge {{ display:inline-block; padding:2px 10px; border:1px solid #a97c22; color:#8a6716; border-radius:3px; font-size:12px; letter-spacing:.06em; }}
.muted {{ color:#5a6675; }}
.cover {{ border-bottom:3px double #23405f; padding-bottom:18px; margin-bottom:8px; }}
figure.sheet {{ margin:12px 0; }}
figure.sheet figcaption {{ font-size:12px; color:#5a6675; margin-bottom:4px; }}
figure.sheet svg {{ max-width:100%; height:auto; border:1px solid #d4d9e0; }}
ul.open {{ margin:4px 0 0; padding-left:20px; }}
.provenance {{ margin-top:36px; padding-top:12px; border-top:1px solid #d4d9e0; font-size:11px; color:#5a6675; }}
@media print {{ body {{ background:#fff; }} .page {{ padding:0; }} }}
</style>
</head>
<body><div class="page">
<div class="cover">
  <h1>{e(session.label)}</h1>
  <span class="badge">{e(status_text)}</span>
  <p class="muted">プロジェクト <code>{e(session.document_id)}</code> ／ 発行 {e(generated_at_utc)}</p>
</div>
<section id="summary">
  <h2>設計概要</h2>
  <table><thead><tr><th>名称</th><th>ID</th><th>種別</th><th>役割</th>
  <th>X [m]</th><th>Y [m]</th><th>Z [m]</th><th>方位角</th><th>干渉形状基準</th>
  </tr></thead><tbody>{entity_rows}</tbody></table>
</section>
<section id="drawings">
  <h2>図面</h2>
  {drawing_blocks or '<p class="muted">この権威から生成できる図面はありません。</p>'}
</section>
{comparison_block}
<section id="sections">
  <h2>算出項目の状態</h2>
  <table><thead><tr><th>項目</th><th>状態</th><th>理由</th></tr></thead>
  <tbody>{section_rows}</tbody></table>
</section>
<section id="open">
  <h2>未確定事項・前提</h2>
  <ul class="open">{open_block or '<li>なし</li>'}</ul>
</section>
{decisions_block}
<div class="provenance">
  <strong>出典・整合性</strong><br>
  プレゼンセッション <code>{e(session.session_id)}</code>
  sha256 <code>{e(session.session_sha256)}</code><br>
  シーンリビジョン <code>{e(authority.scene_revision_id)}</code>
  内容ハッシュ <code>{e(authority.scene_content_hash)}</code><br>
  有効内容ハッシュ <code>{e(authority.effective_scene_content_hash)}</code><br>
  インストール出力 sha256 <code>{e(output.semantic_sha256)}</code><br>
  {'システムバリアント <code>' + e(authority.system_variant_id or '') + '</code><br>' if authority.system_variant_id else ''}
  生成: {e(PACKAGE_GENERATOR)} — 本書は権威データから機械生成されました。
  手書きスクリーンショットやデモデータを含みません。
</div>
</div></body></html>
"""


def _write_member(
    output_dir: Path, relative: str, content: bytes
) -> tuple[PurePosixPath, str, int]:
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ProposalPackageError(
            f'package member path must be relative: {relative!r}'
        )
    target = output_dir.joinpath(*path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    import hashlib

    return path, hashlib.sha256(content).hexdigest(), len(content)


def build_proposal_package(
    session: PresentationSession,
    output_dir: Path | str,
    scene_repository: SceneRepository,
    *,
    presentation_repository: CadPresentationRepository | None = None,
    comparison_repository: CadDesignComparisonRepository | None = None,
    decision_repository: CadDesignDecisionRepository | None = None,
    generated_at_utc: str | None = None,
) -> ProposalPackageResult:
    """Assemble the client-facing proposal document for ``session``.

    All figures come from ``build_installation_output`` over the pinned
    scene (+variant). Comparison content is included only when the
    session pins a comparison set; decisions listed are the document's
    recorded ``DesignDecisionRecord``\\ s.
    """

    output_dir = Path(output_dir)
    generated = generated_at_utc or _utc_now()
    presentation_repository = (
        presentation_repository
        or CadPresentationRepository(scene_repository)
    )
    comparison_repository = (
        comparison_repository
        or CadDesignComparisonRepository(scene_repository)
    )
    decision_repository = (
        decision_repository or CadDesignDecisionRepository(scene_repository)
    )

    warnings: list[str] = []

    revision = scene_repository.get(session.scene_revision_id)
    if revision is None:
        raise ProposalPackageError(
            'session scene revision does not resolve'
        )
    if revision.content_hash != session.scene_content_hash:
        raise ProposalPackageError(
            'session scene content hash mismatch'
        )
    variant = None
    if session.system_variant_id is not None:
        variant = presentation_repository.variant_repository.get_variant(
            session.system_variant_id
        )
        if variant is None:
            raise ProposalPackageError(
                'session SystemVariant does not resolve'
            )

    output = build_installation_output(revision, variant=variant)

    # -- Drawing sheets ----------------------------------------------------
    drawings_svg: list[tuple[str, str]] = []
    try:
        spec = build_drawing_set_spec(
            spec_id=f'proposal-{session.session_id}',
            spec_version='1',
            sheets=(
                'floor_plan',
                'front_elevation',
                'side_elevation',
                'rcp',
            ),
        )
        drawing_set = generate_drawing_set(
            output=output,
            spec=spec,
            project_label=session.label,
            generated_at_utc=generated,
        )
        drawings_svg = [
            (sheet.title_block.sheet_title, sheet.to_svg())
            for sheet in drawing_set.sheets
        ]
    except Exception as exc:
        warnings.append(f'図面の生成に失敗: {exc}')

    # -- Comparison --------------------------------------------------------
    comparison_set: DesignComparisonSet | None = None
    availability_rows: list[tuple[str, str, str, str]] = []
    if session.comparison_set_id is not None:
        comparison_set = comparison_repository.get_set(
            session.comparison_set_id
        )
        if comparison_set is None:
            raise ProposalPackageError(
                'session comparison set does not resolve'
            )
        if comparison_set.set_sha256 != session.comparison_set_sha256:
            raise ProposalPackageError(
                'session comparison set hash mismatch'
            )
        resolved_evidence: dict[tuple[str, str], Any] = {}
        resolver = comparison_repository.ref_resolver
        for alternative in comparison_set.alternatives:
            for ref in alternative.evidence_refs:
                resolver_kind = {
                    'prediction': 'prediction',
                    'measurement': 'measurement',
                    'validation': 'validation',
                    'standards': 'standards',
                    'robustness': 'robustness',
                    'design_checkpoint': 'design_checkpoint',
                }.get(ref.kind)
                if resolver_kind is None or not resolver.knows(resolver_kind):
                    resolved_evidence[(ref.kind, ref.ref_id)] = None
                    continue
                resolved_evidence[(ref.kind, ref.ref_id)] = resolver.resolve(
                    resolver_kind,
                    ref.ref_id,
                    comparison_set.document_id,
                )
        availability = evaluate_comparison_set(
            comparison_set, resolved_evidence=resolved_evidence
        )
        label_of = {
            item.alternative_id: item.label
            for item in comparison_set.alternatives
        }
        availability_rows = [
            (label_of.get(item.alternative_id, item.alternative_id),
             item.kind, item.state, item.reason)
            for item in availability.items
        ]

    decisions = decision_repository.list_decisions(session.document_id)

    document_html = _proposal_html(
        session,
        output,
        tuple(drawings_svg),
        comparison_set,
        tuple(availability_rows),
        decisions,
        generated,
        warnings,
    )

    entries: list[ProposalPackageEntry] = []

    rel, sha, size = _write_member(
        output_dir, 'proposal.html', document_html.encode('utf-8')
    )
    entries.append(
        ProposalPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='document'
        )
    )

    semantic = output.model_dump_json(indent=2).encode('utf-8')
    rel, sha, size = _write_member(
        output_dir, 'installation-output.json', semantic
    )
    entries.append(
        ProposalPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='semantic'
        )
    )

    manifest_fields: dict[str, Any] = {
        'package_id': f'{PROPOSAL_PACKAGE_SCHEMA}:{session.session_id}',
        'generated_at_utc': generated,
        'generator': PACKAGE_GENERATOR,
        'session_id': session.session_id,
        'session_sha256': session.session_sha256,
        'session_label': session.label,
        'status_label': session.status_label,
        'document_id': session.document_id,
        'scene_revision_id': session.scene_revision_id,
        'scene_content_hash': session.scene_content_hash,
        'installation_output_sha256': output.semantic_sha256,
        'system_variant_id': session.system_variant_id,
        'system_variant_sha256': session.system_variant_sha256,
        'comparison_set_id': session.comparison_set_id,
        'comparison_set_sha256': session.comparison_set_sha256,
        'entries': tuple(entries),
    }
    provisional = ProposalPackageManifest.model_construct(
        **manifest_fields, manifest_sha256='0' * 64
    )
    manifest = ProposalPackageManifest(
        **manifest_fields,
        manifest_sha256=_hash(provisional.semantic_payload()),
    )

    rel, sha, size = _write_member(
        output_dir,
        'manifest.json',
        manifest.model_dump_json(indent=2).encode('utf-8'),
    )
    entries.append(
        ProposalPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='manifest'
        )
    )

    return ProposalPackageResult(
        output_dir=str(output_dir), manifest=manifest, warnings=tuple(warnings)
    )


def verify_proposal_package(
    package_dir: Path | str,
) -> ProposalPackageManifest:
    """Re-hash every manifest entry in a proposal package directory."""

    package_dir = Path(package_dir)
    manifest_path = package_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise ProposalPackageError('manifest.json is missing')
    manifest = ProposalPackageManifest.model_validate_json(
        manifest_path.read_text(encoding='utf-8')
    )
    import hashlib

    for entry in manifest.entries:
        if entry.kind == 'manifest':
            continue
        target = package_dir.joinpath(*PurePosixPath(entry.path).parts)
        if not target.is_file():
            raise ProposalPackageError(
                f'package entry is missing: {entry.path}'
            )
        content = target.read_bytes()
        if len(content) != entry.byte_length:
            raise ProposalPackageError(
                f'package entry size mismatch: {entry.path}'
            )
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ProposalPackageError(
                f'package entry hash mismatch: {entry.path}'
            )
    return manifest


__all__ = [
    'PROPOSAL_PACKAGE_KIND',
    'PROPOSAL_PACKAGE_SCHEMA',
    'PROPOSAL_PACKAGE_SCHEMA_VERSION',
    'ProposalPackageEntry',
    'ProposalPackageError',
    'ProposalPackageManifest',
    'ProposalPackageResult',
    'build_proposal_package',
    'verify_proposal_package',
]
