"""AV architectural-drawing symbol profile authority (issue #742).

ANSI J-STD-710-2015 (Audio, Video and Control Architectural Drawing
Symbols) is the production standard; BSR/AVIXA J-STD-710-202x is a
separate in-progress revision and must not be conflated. AVIXA's
downloadable symbol package requires contacting the standards owner
to incorporate symbols into software — HTDT must not silently bundle
or claim standard symbols without rights provenance.

Basis: ANSI J-STD-710-2015 (AVIXA); BSR/AVIXA J-STD-710-202x project
notice (ANSI Standards Action 2026-02-13).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


_EDITIONS = ('jstd710_2015', 'jstd710_202x_draft', 'other', 'unknown')
_RIGHTS_PROVENANCE = (
    # 'licensed_pack' — rights obtained per AVIXA's contact requirement;
    # 'own_drawn' — HTDT-authored lookalike geometry (never claims the
    # standard's symbols); 'public_domain' — verified free asset;
    # 'unknown' — provenance undeclared (fail-closed).
    'licensed_pack', 'own_drawn', 'public_domain', 'unknown',
)
_REVISION_STATES = ('production', 'draft_project', 'withdrawn')

SymbolVerdict = Literal[
    'mapped_current',
    'revision_mismatch',
    'unlicensed_symbols',
    'unknown_symbol',
    'unmapped_device',
]


class ArchitecturalDrawingSymbolProfile(BaseModel):
    """One symbol-set profile pinned to an edition + rights provenance
    (#742). Draft and production editions are distinct identities.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    edition: Literal[
        'jstd710_2015', 'jstd710_202x_draft', 'other', 'unknown'
    ]
    revision_state: Literal[
        'production', 'draft_project', 'withdrawn'
    ]
    rights_provenance: Literal[
        'licensed_pack', 'own_drawn', 'public_domain', 'unknown'
    ]
    rights_evidence_ref: AuthorityRef | None = None
    symbol_set_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('edition') not in _EDITIONS:
                raise ValueError('unknown symbol-standard edition')
            if data.get('revision_state') not in _REVISION_STATES:
                raise ValueError('unknown revision state')
            if data.get('rights_provenance') not in _RIGHTS_PROVENANCE:
                raise ValueError('unknown rights provenance')
            # A draft edition must never masquerade as production.
            if (
                data.get('edition') == 'jstd710_202x_draft'
                and data.get('revision_state') == 'production'
            ):
                raise ValueError(
                    'the 202x BSR draft is not the production edition'
                )
            if (
                data.get('rights_provenance') == 'licensed_pack'
                and data.get('rights_evidence_ref') is None
            ):
                raise ValueError(
                    'licensed symbol packs require a pinned rights '
                    'evidence reference'
                )
            if (
                data.get('rights_provenance') in
                ('licensed_pack', 'public_domain')
                and data.get('symbol_set_ref') is None
            ):
                raise ValueError(
                    'symbol assets require a pinned symbol set '
                    'reference'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ArchitecturalDrawingSymbolProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'ads'
        )


class DeviceSymbolMapping(BaseModel):
    """Maps one HTDT device kind to a standard symbol + revision
    (#742). Unmapped device kinds stay honest."""

    model_config = ConfigDict(frozen=True)

    mapping_id: str
    mapping_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    device_kind: str
    symbol_id: str | None = None
    symbol_revision: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError('mapping requires a pinned profile')
            if not data.get('device_kind'):
                raise ValueError('mapping requires a device kind')
            if (
                data.get('symbol_id')
                and data.get('symbol_revision') is None
            ):
                raise ValueError(
                    'a mapped symbol must record its revision'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'mapping_id', 'mapping_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DeviceSymbolMapping':
        return _seal(
            cls, payload, 'mapping_id', 'mapping_sha256', 'dsm'
        )


class DrawingExportRecord(BaseModel):
    """A construction-drawing export under a pinned symbol profile —
    records the edition and provenance actually used (#742)."""

    model_config = ConfigDict(frozen=True)

    export_id: str
    export_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    mapping_refs: tuple[AuthorityRef, ...]
    export_format: str
    artifact_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError('export requires a pinned profile')
            if not data.get('mapping_refs'):
                raise ValueError(
                    'an export without symbol mappings is not a '
                    'standards-mapped drawing'
                )
            if not data.get('export_format'):
                raise ValueError('export requires a format')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'export_id', 'export_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DrawingExportRecord':
        return _seal(
            cls, payload, 'export_id', 'export_sha256', 'dexp'
        )


def evaluate_symbol_claim(
    profile: ArchitecturalDrawingSymbolProfile | None,
    mapping: DeviceSymbolMapping | None,
    *,
    export_edition: str | None = None,
) -> tuple[SymbolVerdict, str]:
    """Judge whether a drawing may claim standard AV symbols (#742).

    - no profile → 'unmapped_device'
    - rights 'unknown' → 'unlicensed_symbols' (AVIXA pack requires
      contact/licensing; silence is not license)
    - export edition ≠ profile edition → 'revision_mismatch'
    - device kind with no symbol → 'unknown_symbol'
    """
    if profile is None or mapping is None:
        return 'unmapped_device', 'no symbol profile or mapping pinned'
    if profile.rights_provenance == 'unknown':
        return (
            'unlicensed_symbols',
            'symbol rights undeclared — AVIXA pack requires license',
        )
    if (
        export_edition is not None
        and export_edition != profile.edition
    ):
        return (
            'revision_mismatch',
            f'export claims {export_edition} but profile is '
            f'{profile.edition}',
        )
    if not mapping.symbol_id:
        return (
            'unknown_symbol',
            f'no standard symbol for {mapping.device_kind}',
        )
    return (
        'mapped_current',
        f'{mapping.device_kind} → {mapping.symbol_id} '
        f'({profile.edition})',
    )


SYMBOL_LABELS: dict[str, str] = {
    'mapped_current': '規格シンボル割当済み',
    'revision_mismatch': '版不一致',
    'unlicensed_symbols': 'シンボル権利未確認',
    'unknown_symbol': '対応シンボルなし',
    'unmapped_device': '機器↔シンボル未割当',
}
