"""Typed-repository → hub adapters for :class:`ReferenceLibraryIndex` (#630).

Each adapter maps one domain authority listing onto ``LibraryEntry`` without
copying authority: identity/version/scope come straight from the domain
record's own semantic fields, and ``authority_hash`` is the record's
self-verifying semantic digest (never a UI-computed hash).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from .cad_material_library import MaterialDefinition
from .cad_material_library_repository import CadMaterialLibraryRepository
from .cad_speaker_library import SpeakerDefinition
from .cad_speaker_library_repository import CadSpeakerLibraryRepository
from .cad_standards import StandardsProfile
from .cad_standards_repository import CadStandardsRepository
from .equipment_library import EquipmentLibraryService
from .reference_libraries import (
    LibraryEntry,
    LibraryFamily,
    LibraryMetaStore,
    LibraryScope,
    ReferenceLibraryIndex,
)
from .standards_profile_editor import StandardsProfileLibraryService


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
    return LibraryEntry(
        identity=definition.speaker_id,
        family=LibraryFamily.EQUIPMENT,
        scope=(
            LibraryScope.USER_LIBRARY
            if definition.document_id is None
            else LibraryScope.PROJECT_LOCAL
        ),
        display_name=display or definition.speaker_id,
        version=definition.authority_version,
        authority_hash=definition.speaker_sha256,
        source_summary='speaker-definition',
    )


def _material_entry(definition: MaterialDefinition) -> LibraryEntry:
    return LibraryEntry(
        identity=definition.material_id,
        family=LibraryFamily.MATERIAL,
        scope=(
            LibraryScope.USER_LIBRARY
            if definition.document_id is None
            else LibraryScope.PROJECT_LOCAL
        ),
        display_name=definition.name,
        version=definition.authority_version,
        authority_hash=definition.material_sha256,
        capability_summary=str(definition.category),
        source_summary=definition.manufacturer,
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

    index = ReferenceLibraryIndex(
        LibraryMetaStore.for_data_dir(Path(data_dir))
    )

    def equipment_and_speakers() -> Iterable[LibraryEntry]:
        for definition in equipment.definitions():
            yield _equipment_entry(definition)
        for definition in speakers.list_speakers():
            yield _speaker_entry(definition)

    index.register_provider(
        _ListingProvider(
            LibraryFamily.EQUIPMENT,
            equipment_and_speakers,
            editor_hint='機材ライブラリ',
        )
    )
    index.register_provider(
        _ListingProvider(
            LibraryFamily.MATERIAL,
            lambda: (
                _material_entry(item) for item in materials.list_materials()
            ),
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
