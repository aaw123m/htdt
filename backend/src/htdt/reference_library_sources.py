"""Typed-repository → hub adapters for :class:`ReferenceLibraryIndex` (#630).

Each adapter maps one domain authority listing onto ``LibraryEntry`` without
copying authority: identity/version/scope come straight from the domain
record's own semantic fields, and ``authority_hash`` is the record's
self-verifying semantic digest (never a UI-computed hash).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from .cad_material_library import BUILTIN_MATERIAL_IDS, MaterialDefinition
from .cad_material_library_repository import CadMaterialLibraryRepository
from .cad_projector_reference_pack import PROJECTOR_REFERENCE_PACKS
from .cad_screen_evidence_registry import PROJECTION_SCREEN_EVIDENCE_REGISTRY
from .cad_speaker_library import BUILTIN_SPEAKER_IDS, SpeakerDefinition
from .cad_speaker_library_repository import CadSpeakerLibraryRepository
from .cad_standards import StandardsProfile
from .cad_tactile_reference_pack import TACTILE_REFERENCE_PACK
from .canonical_json import canonical_sha256
from .cad_standards_repository import CadStandardsRepository
from .equipment_library_service import EquipmentLibraryService
from .reference_libraries import (
    LibraryEntry,
    LibraryFamily,
    LibraryMetaStore,
    LibraryScope,
    ReferenceLibraryIndex,
)
from .standards_profile_library_service import StandardsProfileLibraryService


class _ListingProvider:
    """``LibraryProvider`` over a plain entries callable."""

    def __init__(
        self,
        family: LibraryFamily,
        source: Callable[[], Iterable[LibraryEntry]],
        editor_hint: str | None = None,
    ) -> None:
        self.family = family
        self._source = source
        self._editor_hint = editor_hint

    def list_entries(self) -> Iterable[LibraryEntry]:
        return tuple(self._source())

    def open_editor_hint(self, entry: LibraryEntry) -> str | None:
        return self._editor_hint


def _equipment_entry(definition) -> LibraryEntry:
    display = (
        definition.user_label
        or " ".join(
            part
            for part in (definition.manufacturer, definition.model)
            if part
        )
        or definition.definition_id
    )
    return LibraryEntry(
        identity=definition.definition_id,
        family=LibraryFamily.EQUIPMENT,
        scope=(
            LibraryScope.USER_LIBRARY
            if definition.identity_kind == 'user_defined'
            else LibraryScope.BUILTIN
        ),
        display_name=display,
        version=definition.version,
        authority_hash=definition.semantic_sha256,
        capability_summary=definition.identity_kind,
    )


def _speaker_entry(definition: SpeakerDefinition) -> LibraryEntry:
    display = " ".join(
        part
        for part in (definition.manufacturer, definition.model, definition.variant)
        if part
    )
    if definition.speaker_id in BUILTIN_SPEAKER_IDS:
        scope = LibraryScope.BUILTIN
    elif definition.document_id is None:
        scope = LibraryScope.USER_LIBRARY
    else:
        scope = LibraryScope.PROJECT_LOCAL
    return LibraryEntry(
        identity=definition.speaker_id,
        family=LibraryFamily.EQUIPMENT,
        scope=scope,
        display_name=display or definition.speaker_id,
        version=definition.authority_version,
        authority_hash=definition.speaker_sha256,
        source_summary='speaker-definition',
    )


def _material_entry(definition: MaterialDefinition) -> LibraryEntry:
    if definition.material_id in BUILTIN_MATERIAL_IDS:
        scope = LibraryScope.BUILTIN
    elif definition.document_id is None:
        scope = LibraryScope.USER_LIBRARY
    else:
        scope = LibraryScope.PROJECT_LOCAL
    return LibraryEntry(
        identity=definition.material_id,
        family=LibraryFamily.MATERIAL,
        scope=scope,
        display_name=definition.name,
        version=definition.authority_version,
        authority_hash=definition.material_sha256,
        capability_summary=str(definition.category),
        source_summary=definition.manufacturer,
    )


def _projector_entry(pack) -> LibraryEntry:
    return LibraryEntry(
        identity=f'projector-reference:{pack.pack_id}',
        family=LibraryFamily.EQUIPMENT,
        scope=LibraryScope.BUILTIN,
        display_name=f'{pack.manufacturer} {pack.model}',
        description='プロジェクタースペック・リファレンス',
        version=pack.authority_version,
        authority_hash=pack.semantic_sha256,
        capability_summary=(
            f'{len(pack.assertions)} 項目検証済み / '
            f'{len(pack.unknown_fields)} 項目不明'
        ),
        source_summary=', '.join(source.source_id for source in pack.sources),
    )


def _tactile_entry(reference) -> LibraryEntry:
    return LibraryEntry(
        identity=f'tactile-reference:{reference.reference_id}',
        family=LibraryFamily.EQUIPMENT,
        scope=LibraryScope.BUILTIN,
        display_name=f'{reference.manufacturer} {reference.model}',
        description='タクタイルアクチュエーター・リファレンス',
        version='tactile-reference-1',
        authority_hash=reference.semantic_sha256,
        capability_summary='tactile-actuator',
        source_summary=reference.datasheet_title,
    )


def _screen_entries() -> Iterable[LibraryEntry]:
    registry = PROJECTION_SCREEN_EVIDENCE_REGISTRY
    for model in sorted({record.screen_model for record in registry.records}):
        records = tuple(
            record for record in registry.records if record.screen_model == model
        )
        yield LibraryEntry(
            identity=f'screen-evidence:{model}',
            family=LibraryFamily.MATERIAL,
            scope=LibraryScope.BUILTIN,
            display_name=model,
            description='投影スクリーン音響証拠',
            version=registry.authority_version,
            authority_hash=canonical_sha256(
                [record.semantic_sha256 for record in records]
            ),
            capability_summary=f'{len(records)} 件の測定・証拠記録',
            source_summary=', '.join(
                sorted({record.source_id for record in records})
            ),
        )


def _profile_entry(profile: StandardsProfile) -> LibraryEntry:
    return LibraryEntry(
        identity=profile.profile_id,
        family=LibraryFamily.STANDARD_PROFILE,
        scope=(
            LibraryScope.BUILTIN
            if profile.profile_kind == 'published'
            else LibraryScope.USER_LIBRARY
        ),
        display_name=profile.name,
        version=profile.version,
        authority_hash=profile.profile_semantic_hash,
        capability_summary=str(profile.profile_kind),
    )


def build_reference_library_index(
    scene_repository,
    data_dir: Path,
) -> ReferenceLibraryIndex:
    """Assemble the hub index over every typed authority the app persists.

    The equipment family aggregates equipment definitions and speaker
    definitions — both are source authorities — while materials and
    standards profiles keep their own families. The meta store carries
    app-local archive flags next to the data root.
    """

    equipment = EquipmentLibraryService(scene_repository)
    speakers = CadSpeakerLibraryRepository(scene_repository)
    materials = CadMaterialLibraryRepository(scene_repository)
    profiles = StandardsProfileLibraryService(
        CadStandardsRepository(scene_repository)
    )
    # Bootstrap the bundled catalogs into the shared scope so the curated
    # authorities actually reach the hub and pickers (idempotent).
    speakers.install_builtin_library()
    materials.install_builtin_library()

    index = ReferenceLibraryIndex(
        LibraryMetaStore.for_data_dir(Path(data_dir))
    )

    def equipment_and_speakers() -> Iterable[LibraryEntry]:
        for definition in equipment.definitions():
            yield _equipment_entry(definition)
        for definition in speakers.list_speakers():
            yield _speaker_entry(definition)
        for pack in PROJECTOR_REFERENCE_PACKS:
            yield _projector_entry(pack)
        for reference in TACTILE_REFERENCE_PACK:
            yield _tactile_entry(reference)

    index.register_provider(
        _ListingProvider(
            LibraryFamily.EQUIPMENT,
            equipment_and_speakers,
            editor_hint='機材ライブラリ',
        )
    )
    def material_entries() -> Iterable[LibraryEntry]:
        for item in materials.list_materials():
            yield _material_entry(item)
        yield from _screen_entries()

    index.register_provider(
        _ListingProvider(
            LibraryFamily.MATERIAL,
            material_entries,
            editor_hint='音響材料ライブラリ',
        )
    )
    index.register_provider(
        _ListingProvider(
            LibraryFamily.STANDARD_PROFILE,
            lambda: (_profile_entry(item) for item in profiles.profiles()),
            editor_hint='基準プロファイル',
        )
    )
    return index


__all__ = ["build_reference_library_index"]
