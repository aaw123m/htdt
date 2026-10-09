"""Cross-search / detail / compare read model for the Reference Library.

Issue #990: the Reference Library page composes this module over
:class:`ReferenceLibraryIndex` plus the same typed repositories the shared
hub is built on. Everything here is read-only: rows keep the domain
record's own identity/version/hash, the detail resolver reads the
canonical record by its semantic hash, and usage sites come straight from
the binding/instance repositories' own resolution fields. Nothing is
merged or re-keyed — two entries with the same name stay distinct rows
keyed by ``semantic_key``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .cad_current_equipment import resolve_current_equipment_binding
from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_equipment_instance_repository import CadInstalledEquipmentRepository
from .cad_material_library_repository import CadMaterialLibraryRepository
from .cad_projector_reference_pack import PROJECTOR_REFERENCE_PACKS
from .cad_speaker_library_repository import CadSpeakerLibraryRepository
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_tactile_reference_pack import TACTILE_REFERENCE_PACK
from .equipment_library import EquipmentLibraryService
from .reference_libraries import (
    LibraryEntry,
    LibraryFamily,
    ReferenceLibraryIndex,
)
from .standards_profile_editor import StandardsProfileLibraryService


def fold_text(value: str) -> str:
    """NFKC + casefold — same normalization ``index.search`` applies."""

    return unicodedata.normalize('NFKC', value).casefold()


# --- Record-kind labels -------------------------------------------------

#: EquipmentDataProvenance.evidence_kind → JA evidence class label.
_EVIDENCE_KIND_LABELS = {
    'measured': '実測値',
    'manufacturer': '製品データ',
    'analytic': '設計値（解析）',
    'inferred': '設計値（推定）',
    'user_defined': 'ユーザー定義',
}

#: Material/Speaker provenance_class → JA evidence class label.
_PROVENANCE_CLASS_LABELS = {
    'manufacturer_measured': '実測値（メーカー）',
    'laboratory_measured': '実測値（ラボ）',
    'independent_published': '公開データ',
    'user_measured': '実測値（ユーザー）',
    'user_in_situ_measured': '実測値（現場計測）',
    'analytic_model': '設計値（解析）',
    'inferred_estimated': '設計値（推定）',
    'generic_reference_preset': '参照プリセット',
}

_CATEGORY_LABELS = {
    'manufacturer': 'メーカー登録',
    'user_defined': 'ユーザー定義',
    'published': '公開',
    'user': 'ユーザー',
}


@dataclass(frozen=True, slots=True)
class LibraryUsageSite:
    """One place the current project references a library authority.

    ``kind`` is the navigation-target vocabulary: ``'scene_entity'`` for a
    bound speaker entity, ``'installed_equipment_instance'`` for an
    installed unit. ``resolution`` keeps the authority's verbatim token
    (``explicit_binding`` / ``variant_application`` / ``RESOLVED_EXACT`` /
    ``EXTERNAL_UNRESOLVED`` …) — honesty over paraphrase.
    """

    kind: str
    target_id: str
    label: str
    resolution: str
    role: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryRecordDetail:
    """Detail projection resolved from the canonical domain record.

    ``record_missing`` marks an index row whose domain record no longer
    resolves — such entries are never presented as qualified for adoption.
    """

    sources: tuple[str, ...] = ()
    evidence_labels: tuple[str, ...] = ()
    rights_label: str = '権利情報なし'
    fields: tuple[tuple[str, str], ...] = ()
    record_missing: bool = False


@dataclass(frozen=True, slots=True)
class LibraryRow:
    """One searchable row — one ``LibraryEntry`` plus resolved detail."""

    entry: LibraryEntry
    semantic_key: str
    family: str
    archived: bool
    superseded: bool
    attention: bool
    record_missing: bool
    qualified: bool
    category: str
    sources: tuple[str, ...]
    evidence_labels: tuple[str, ...]
    rights_label: str
    detail_fields: tuple[tuple[str, str], ...]
    usage_sites: tuple[LibraryUsageSite, ...]
    search_blob: str


@dataclass(frozen=True, slots=True)
class LibraryComparison:
    """Field-by-field diff plus dependents at risk on a swap.

    ``dependents`` are the current project's usage sites that pin each
    entry's exact semantic key — a swap leaves them pointing at the old
    version, which is exactly what the UI must surface.
    """

    fields: tuple[tuple[str, str, str], ...]
    dependents_a: tuple[LibraryUsageSite, ...]
    dependents_b: tuple[LibraryUsageSite, ...]


# --- Row collection -----------------------------------------------------

_DETAIL_FIELD_LABELS = {
    'name': '名前',
    'family': '種別',
    'category': '区分',
    'identity': 'ID',
    'version': 'バージョン',
    'scope': 'スコープ',
    'status': '状態',
    'sources': '出典',
    'evidence': '根拠区分',
    'rights': '権利',
    'hash': 'SHA-256',
    'semantic_key': '参照キー',
    'missing': '欠けている根拠',
}

FAMILY_TITLES = {
    'equipment': '機材・スピーカー定義',
    'treatment': '吸音・処理材',
    'target_curve': '目標カーブ',
    'standard_profile': '基準プロファイル',
    'instrument': '測定機器',
    'material': '音響材料',
    'operating_profile': '動作プロファイル',
}

SCOPE_LABELS = {
    'builtin': '同梱',
    'user_library': 'ユーザーライブラリ',
    'project_local': 'プロジェクト',
    'imported_dependency': '依存として取り込み',
    'historical': '履歴',
}


def category_label(raw: str) -> str:
    """JA label for a capability/category token; verbatim when unknown."""

    return _CATEGORY_LABELS.get(raw, raw)


def row_status_label(row: LibraryRow) -> str:
    """Honest status badges — stale/missing/archived are always shown."""

    parts: list[str] = []
    parts.append('最新' if row.entry.is_latest else '旧版')
    if row.entry.missing_dependencies:
        parts.append('根拠不足')
    if row.record_missing:
        parts.append('記録なし')
    if row.archived:
        parts.append('アーカイブ')
    return '・'.join(parts)


def collect_library_rows(
    index: ReferenceLibraryIndex,
    *,
    detail_resolver: Callable[[LibraryEntry], LibraryRecordDetail] | None = None,
    usage_sites: Mapping[str, tuple[LibraryUsageSite, ...]] | None = None,
) -> tuple[LibraryRow, ...]:
    """Materialize one row per index entry (including archived).

    ``detail_resolver`` maps an entry to the canonical domain record's
    projection; ``usage_sites`` maps ``semantic_key`` → current-project
    usage sites. Both are injected so tests can drive the model without
    repositories.
    """

    usage_sites = usage_sites or {}
    rows: list[LibraryRow] = []
    all_entries: list[LibraryEntry] = []
    for family in index.families():
        all_entries.extend(
            index.entries(family=family, include_archived=True)
        )
    # Latest marker per (family, identity): the entry declaring is_latest,
    # else the highest version string. Any other version under the same
    # identity is an older version — shown as 旧版, never merged away.
    by_identity: dict[tuple[str, str], list[LibraryEntry]] = {}
    for entry in all_entries:
        by_identity.setdefault(
            (str(entry.family), entry.identity), []
        ).append(entry)
    latest_keys: set[str] = set()
    for group in by_identity.values():
        claimed = [item for item in group if item.is_latest]
        latest = max(
            claimed or group, key=lambda item: item.version
        )
        latest_keys.add(latest.semantic_key)
    for entry in all_entries:
        detail = (
            detail_resolver(entry)
            if detail_resolver is not None
            else LibraryRecordDetail()
        )
        superseded = (
            not entry.is_latest or entry.semantic_key not in latest_keys
        )
        archived = index.is_archived(entry)
        attention = superseded or bool(entry.missing_dependencies)
        record_missing = detail.record_missing
        qualified = (
            not archived
            and not record_missing
            and not entry.missing_dependencies
        )
        category = entry.capability_summary or ''
        sources = detail.sources or (
            (entry.source_summary,) if entry.source_summary else ()
        )
        sites = usage_sites.get(entry.semantic_key, ())
        status = row_status_label_raw(
            entry=entry,
            archived=archived,
            record_missing=record_missing,
        )
        fields: list[tuple[str, str]] = [
            (_DETAIL_FIELD_LABELS['name'], entry.display_name),
            (
                _DETAIL_FIELD_LABELS['family'],
                FAMILY_TITLES.get(str(entry.family), str(entry.family)),
            ),
            (_DETAIL_FIELD_LABELS['category'], category_label(category)),
            (_DETAIL_FIELD_LABELS['identity'], entry.identity),
            (_DETAIL_FIELD_LABELS['version'], entry.version),
            (_DETAIL_FIELD_LABELS['status'], status),
            (
                _DETAIL_FIELD_LABELS['scope'],
                SCOPE_LABELS.get(str(entry.scope), str(entry.scope)),
            ),
            (
                _DETAIL_FIELD_LABELS['sources'],
                '、'.join(sources) if sources else '—',
            ),
            (
                _DETAIL_FIELD_LABELS['evidence'],
                '、'.join(detail.evidence_labels)
                if detail.evidence_labels
                else '記録なし',
            ),
            (_DETAIL_FIELD_LABELS['rights'], detail.rights_label),
            (_DETAIL_FIELD_LABELS['hash'], entry.authority_hash),
            (_DETAIL_FIELD_LABELS['semantic_key'], entry.semantic_key),
        ]
        fields.extend(detail.fields)
        if entry.missing_dependencies:
            fields.append(
                (
                    _DETAIL_FIELD_LABELS['missing'],
                    '、'.join(entry.missing_dependencies),
                )
            )
        blob_parts = [
            entry.display_name,
            entry.identity,
            entry.version,
            str(entry.family),
            str(entry.scope),
            FAMILY_TITLES.get(str(entry.family), ''),
            category,
            category_label(category),
            entry.source_summary or '',
            entry.description or '',
            entry.authority_hash,
            '、'.join(sources),
            '、'.join(detail.evidence_labels),
        ]
        blob_parts.extend(value for _label, value in fields)
        for site in sites:
            blob_parts.extend((site.label, site.role or '', site.resolution))
        rows.append(
            LibraryRow(
                entry=entry,
                semantic_key=entry.semantic_key,
                family=str(entry.family),
                archived=archived,
                superseded=superseded,
                attention=attention,
                record_missing=record_missing,
                qualified=qualified,
                category=category,
                sources=sources,
                evidence_labels=detail.evidence_labels,
                rights_label=detail.rights_label,
                detail_fields=tuple(fields),
                usage_sites=sites,
                search_blob=fold_text(' '.join(blob_parts)),
            )
        )
    return tuple(rows)


def row_status_label_raw(
    *,
    entry: LibraryEntry,
    archived: bool,
    record_missing: bool,
) -> str:
    parts: list[str] = []
    parts.append('最新' if entry.is_latest else '旧版')
    if entry.missing_dependencies:
        parts.append('根拠不足')
    if record_missing:
        parts.append('記録なし')
    if archived:
        parts.append('アーカイブ')
    return '・'.join(parts)


# --- Filtering ------------------------------------------------------------

STATUS_ALL = 'all'
STATUS_LATEST = 'latest'
STATUS_ATTENTION = 'attention'
STATUS_UNQUALIFIED = 'unqualified'


def filter_rows(
    rows: tuple[LibraryRow, ...] | list[LibraryRow],
    *,
    query: str = '',
    family: str | None = None,
    category: str | None = None,
    source: str | None = None,
    status: str = STATUS_ALL,
) -> list[LibraryRow]:
    """Apply the browser's filter set.

    ``STATUS_ALL`` keeps the existing hide-archived default; archived and
    record-missing rows remain reachable under ``STATUS_UNQUALIFIED``.
    """

    needle = fold_text(query.strip())
    result: list[LibraryRow] = []
    for row in rows:
        if status == STATUS_ALL:
            if row.archived:
                continue
        elif status == STATUS_LATEST:
            if (
                not row.entry.is_latest
                or row.archived
                or not row.qualified
            ):
                continue
        elif status == STATUS_ATTENTION:
            if row.archived or not (row.attention or row.record_missing):
                continue
        elif status == STATUS_UNQUALIFIED:
            if not (row.archived or row.record_missing):
                continue
        if family is not None and row.family != family:
            continue
        if category is not None and row.category != category:
            continue
        if source is not None and source not in row.sources:
            continue
        if needle and needle not in row.search_blob:
            continue
        result.append(row)
    return result


# --- Compare ---------------------------------------------------------------

def compare_rows(a: LibraryRow, b: LibraryRow) -> LibraryComparison | None:
    """Diff two rows of the SAME family; ``None`` means not comparable.

    The pair is never merged: each side keeps its own identity/version/
    hash and its own dependents.
    """

    if a.family != b.family:
        return None
    labels = ('name', 'identity', 'version', 'status', 'scope', 'category',
              'sources', 'evidence', 'rights', 'hash')
    a_fields = dict(a.detail_fields)
    b_fields = dict(b.detail_fields)
    fields: list[tuple[str, str, str]] = []
    for key in labels:
        label = _DETAIL_FIELD_LABELS[key]
        fields.append(
            (
                label,
                a_fields.get(label, '—'),
                b_fields.get(label, '—'),
            )
        )
    return LibraryComparison(
        fields=tuple(fields),
        dependents_a=a.usage_sites,
        dependents_b=b.usage_sites,
    )


# --- Domain-record detail resolver --------------------------------------


def build_reference_library_detail_resolver(
    scene_repository,
    data_dir: Path,
) -> Callable[[LibraryEntry], LibraryRecordDetail]:
    """Snapshot resolver: entry → detail from the canonical record.

    Opens the same repositories as ``build_reference_library_index`` and
    materializes hash→record maps once — the returned callable is pure
    lookup, so a page refresh sees one consistent snapshot. Records that
    no longer resolve produce ``record_missing`` instead of guessing.
    """

    equipment = EquipmentLibraryService(scene_repository)
    speakers = CadSpeakerLibraryRepository(scene_repository)
    materials = CadMaterialLibraryRepository(scene_repository)
    profiles = StandardsProfileLibraryService(
        CadStandardsRepository(scene_repository)
    )

    equipment_by_hash = {
        definition.semantic_sha256: definition
        for definition in equipment.definitions()
    }
    speaker_by_hash = {
        definition.speaker_sha256: definition
        for definition in speakers.list_speakers()
    }
    speaker_datasets = {
        definition.speaker_id: speakers.list_datasets(definition.speaker_id)
        for definition in speaker_by_hash.values()
    }
    material_by_hash = {
        definition.material_sha256: definition
        for definition in materials.list_materials()
    }
    material_evidence = {
        definition.material_id: materials.list_evidence(definition.material_id)
        for definition in material_by_hash.values()
    }
    profile_by_hash = {
        profile.profile_semantic_hash: profile
        for profile in profiles.profiles()
    }
    projector_by_hash = {
        pack.semantic_sha256: pack for pack in PROJECTOR_REFERENCE_PACKS
    }
    tactile_by_hash = {
        reference.semantic_sha256: reference
        for reference in TACTILE_REFERENCE_PACK
    }

    def equipment_detail(definition) -> LibraryRecordDetail:
        sources: list[str] = []
        evidence: list[str] = []
        for item in definition.provenance:
            name = item.source_name
            if item.source_version:
                name = f'{name} {item.source_version}'
            sources.append(name)
            label = _EVIDENCE_KIND_LABELS.get(
                str(item.evidence_kind), str(item.evidence_kind)
            )
            if label not in evidence:
                evidence.append(label)
        maker = definition.manufacturer or '—'
        model = definition.model or definition.user_label or '—'
        return LibraryRecordDetail(
            sources=tuple(sources),
            evidence_labels=tuple(evidence),
            rights_label='権利情報なし',
            fields=(
                ('メーカー', maker),
                ('モデル', model),
                (
                    '定義区分',
                    category_label(str(definition.identity_kind)),
                ),
            ),
        )

    def _licensed_rights(records) -> str:
        values = {
            record.redistribution_permitted for record in records
        }
        names = sorted(
            {
                record.license_name
                for record in records
                if record.license_name
            }
        )
        if not records:
            return '権利情報なし'
        if False in values:
            return '再配布不可・制限あり'
        if values == {True}:
            return f'再配布可（{names[0]}）' if names else '再配布可'
        return '権利未確認'

    def speaker_detail(definition) -> LibraryRecordDetail:
        datasets = speaker_datasets.get(definition.speaker_id, ())
        sources = [
            dataset.source_label
            for dataset in datasets
            if dataset.source_label
        ]
        evidence = []
        for dataset in datasets:
            label = _PROVENANCE_CLASS_LABELS.get(
                str(dataset.provenance_class), str(dataset.provenance_class)
            )
            if label not in evidence:
                evidence.append(label)
        return LibraryRecordDetail(
            sources=tuple(sources),
            evidence_labels=('製品データ', *evidence),
            rights_label=_licensed_rights(datasets),
            fields=(
                ('メーカー', definition.manufacturer or '—'),
                ('モデル', definition.model),
                ('バリアント', definition.variant or '—'),
                ('構成', str(definition.configuration)),
                ('データセット', f'{len(datasets)} 件'),
            ),
        )

    def material_detail(definition) -> LibraryRecordDetail:
        evidence_records = material_evidence.get(
            definition.material_id, ()
        )
        sources = [
            record.source_label
            for record in evidence_records
            if getattr(record, 'source_label', None)
        ]
        evidence = []
        for record in evidence_records:
            label = _PROVENANCE_CLASS_LABELS.get(
                str(record.provenance_class), str(record.provenance_class)
            )
            if label not in evidence:
                evidence.append(label)
        fields: list[tuple[str, str]] = [
            ('メーカー', definition.manufacturer or '—'),
            ('モデル', definition.model or '—'),
            ('カテゴリ', str(definition.category)),
        ]
        if definition.thickness_mm is not None:
            fields.append(('厚さ', f'{definition.thickness_mm} mm'))
        if definition.mounting:
            fields.append(('施工', str(definition.mounting)))
        return LibraryRecordDetail(
            sources=tuple(sources),
            evidence_labels=tuple(evidence),
            rights_label=_licensed_rights(evidence_records),
            fields=tuple(fields),
        )

    def profile_detail(profile) -> LibraryRecordDetail:
        publishers = sorted(
            {
                str(criterion.source.publisher)
                for criterion in profile.criteria
                if getattr(getattr(criterion, 'source', None), 'publisher', None)
            }
        )
        return LibraryRecordDetail(
            sources=tuple(publishers),
            evidence_labels=('規格・基準プロファイル',),
            rights_label='権利情報なし',
            fields=(
                ('種別', str(profile.profile_kind)),
                ('基準項目', f'{len(profile.criteria)} 件'),
            ),
        )

    def pack_detail(pack) -> LibraryRecordDetail:
        return LibraryRecordDetail(
            sources=tuple(source.source_id for source in pack.sources),
            evidence_labels=('製品データ（参照仕様）',),
            rights_label='権利情報なし',
            fields=(
                ('種別', 'プロジェクター・リファレンスパック'),
                ('検証項目', f'{len(pack.assertions)} 項目'),
                ('不明項目', f'{len(pack.unknown_fields)} 項目'),
            ),
        )

    def tactile_detail(reference) -> LibraryRecordDetail:
        return LibraryRecordDetail(
            sources=(reference.datasheet_title,),
            evidence_labels=('製品データ（参照仕様）',),
            rights_label='権利情報なし',
            fields=(('種別', 'タクタイルアクチュエーター・リファレンス'),),
        )

    def screen_detail() -> LibraryRecordDetail:
        return LibraryRecordDetail(
            evidence_labels=('実測・証拠レジストリ',),
            rights_label='権利情報なし',
        )

    def resolve(entry: LibraryEntry) -> LibraryRecordDetail:
        family = str(entry.family)
        key = entry.authority_hash
        if family == str(LibraryFamily.EQUIPMENT):
            definition = equipment_by_hash.get(key)
            if definition is not None:
                return equipment_detail(definition)
            speaker = speaker_by_hash.get(key)
            if speaker is not None:
                return speaker_detail(speaker)
            pack = projector_by_hash.get(key)
            if pack is not None:
                return pack_detail(pack)
            tactile = tactile_by_hash.get(key)
            if tactile is not None:
                return tactile_detail(tactile)
            return LibraryRecordDetail(record_missing=True)
        if family == str(LibraryFamily.MATERIAL):
            material = material_by_hash.get(key)
            if material is not None:
                return material_detail(material)
            if entry.identity.startswith('screen-evidence:'):
                return screen_detail()
            return LibraryRecordDetail(record_missing=True)
        if family == str(LibraryFamily.STANDARD_PROFILE):
            profile = profile_by_hash.get(key)
            if profile is not None:
                return profile_detail(profile)
            return LibraryRecordDetail(record_missing=True)
        return LibraryRecordDetail(record_missing=True)

    return resolve


# --- Current-project usage sites -----------------------------------------


def collect_usage_sites(
    scene_repository,
    document_id: str | None,
) -> dict[str, tuple[LibraryUsageSite, ...]]:
    """semantic_key → sites where the current document uses the authority.

    Two canonical reads only: per-speaker current-equipment resolution
    (#984 resolver — explicit bindings and variant-derived bindings alike)
    and installed equipment instances (#819 typed resolution). Never
    writes, never rebinds.
    """

    if not document_id:
        return {}
    revision = scene_repository.latest(document_id)
    if revision is None:
        return {}
    equipment_repository = EquipmentLibraryService(
        scene_repository
    ).equipment_repository
    binding_repository = CadEquipmentBindingRepository(
        scene_repository, equipment_repository
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    instance_repository = CadInstalledEquipmentRepository(
        scene_repository,
        equipment_repository=equipment_repository,
    )

    sites: dict[str, list[LibraryUsageSite]] = {}

    def add(key: str, site: LibraryUsageSite) -> None:
        bucket = sites.setdefault(key, [])
        if any(
            existing.target_id == site.target_id
            and existing.kind == site.kind
            for existing in bucket
        ):
            return
        bucket.append(site)

    entities = getattr(revision.document, 'entities', ()) or ()
    for entity in entities:
        if getattr(entity, 'kind', None) != 'speaker':
            continue
        resolved = resolve_current_equipment_binding(
            scene_repository=scene_repository,
            variant_repository=variant_repository,
            revision_id=revision.revision_id,
            entity_id=entity.entity_id,
            binding_repository=binding_repository,
        )
        if resolved is None:
            continue
        key = (
            f'{resolved.equipment_definition_id}'
            f'@{resolved.equipment_definition_version}'
            f'#{resolved.equipment_definition_sha256}'
        )
        name = getattr(entity, 'name', None) or entity.entity_id
        role = getattr(entity, 'speaker_role', None)
        label = f'スピーカー {name}' + (f'（{role}）' if role else '')
        add(
            key,
            LibraryUsageSite(
                kind='scene_entity',
                target_id=entity.entity_id,
                label=label,
                resolution=resolved.resolution,
                role=role,
            ),
        )

    for instance in instance_repository.list_instances(document_id):
        resolution = instance_repository.resolve_instance_definition(
            instance.instance_id
        )
        if resolution is None:
            continue
        binding = resolution.binding
        ref = (
            binding.definition_ref
            if binding is not None
            else instance.definition_ref
        )
        if ref is None:
            continue
        key = (
            f'{ref.equipment_definition_id}'
            f'@{ref.equipment_definition_version}'
            f'#{ref.equipment_definition_sha256}'
        )
        label = (
            f'設置機器 {instance.instance_id}'
            f'（{instance.equipment_class}）'
        )
        add(
            key,
            LibraryUsageSite(
                kind='installed_equipment_instance',
                target_id=instance.instance_id,
                label=label,
                resolution=resolution.status,
                role=None,
            ),
        )

    return {
        key: tuple(bucket) for key, bucket in sites.items()
    }


__all__ = [
    'LibraryComparison',
    'LibraryRecordDetail',
    'LibraryRow',
    'LibraryUsageSite',
    'STATUS_ALL',
    'STATUS_ATTENTION',
    'STATUS_LATEST',
    'STATUS_UNQUALIFIED',
    'build_reference_library_detail_resolver',
    'category_label',
    'collect_library_rows',
    'collect_usage_sites',
    'compare_rows',
    'filter_rows',
    'fold_text',
    'row_status_label',
]
