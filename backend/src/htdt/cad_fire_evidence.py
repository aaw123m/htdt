"""Acoustic-finish fire / reaction-to-fire evidence authority
(issue #648).

A material can be acoustically appropriate and geometrically
installable while its reaction-to-fire / interior-finish eligibility
remains unknown or installation-specific. ASTM E84-26a provides
comparative flame-spread/smoke measurements under its test conditions
— it does not by itself classify a material as noncombustible or
give a complete fire-hazard assessment; NFPA 286 measures room-corner
contribution, a different measurand and assembly context. A product
page saying 'Class A' is not installable evidence.

Basis: ASTM E84-26a (surface-burning comparative test; ASTM scope
disclaimer), NFPA 286 (room-corner interior-finish contribution).
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


_TEST_STANDARDS = (
    'astm_e84', 'nfpa_286', 'en_13501_1', 'ul_723', 'iso_9705',
    'other', 'unknown',
)
_ASSEMBLY_PARTS = (
    'absorber', 'fabric_facing', 'stretched_fabric_system',
    'acoustic_foam', 'diffuser_finish', 'screen_fabric',
    'backing_substrate', 'air_gap_config', 'other',
)

FireVerdict = Literal[
    'eligible_assembly',
    'lab_result_is_not_installation',
    'unclaimed_fire_evidence',
    'acoustic_not_safety',
    'test_scope_mismatch',
]


class ReactionToFireEvidence(BaseModel):
    """One pinned fire/reaction-to-fire test record (#648) — the
    standard, the assembly actually tested, and the measured result.
    A result under one standard/assembly never transfers silently to
    another."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_ref: AuthorityRef | None = None
    test_standard: Literal[
        'astm_e84', 'nfpa_286', 'en_13501_1', 'ul_723', 'iso_9705',
        'other', 'unknown',
    ]
    tested_assembly_descriptor: str
    result_descriptor: str
    report_ref: AuthorityRef

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('test_standard') not in _TEST_STANDARDS:
                raise ValueError('unknown fire-test standard')
            if data.get('test_standard') == 'unknown':
                raise ValueError(
                    'fire evidence must name its test standard — an '
                    'unspecified rating is not evidence'
                )
            if not data.get('tested_assembly_descriptor'):
                raise ValueError(
                    'fire evidence must describe the tested assembly '
                    '(material alone is not the installed finish)'
                )
            if data.get('report_ref') is None:
                raise ValueError(
                    'fire evidence requires a pinned report reference'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ReactionToFireEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'rtf'
        )


class FinishAssemblySafetyEvidence(BaseModel):
    """Binds fire evidence to the installation assembly under
    consideration (#648) — declared parts + jurisdiction context."""

    model_config = ConfigDict(frozen=True)

    assembly_id: str
    assembly_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    assembly_parts: tuple[str, ...]
    evidence_refs: tuple[AuthorityRef, ...]
    installation_context: Literal[
        'wall_finish', 'ceiling_finish', 'free_object',
        'behind_screen', 'other',
    ]
    jurisdiction_descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            parts = data.get('assembly_parts') or ()
            if not parts:
                raise ValueError(
                    'a finish assembly must name its parts'
                )
            unknown = [p for p in parts if p not in _ASSEMBLY_PARTS]
            if unknown:
                raise ValueError(f'unknown assembly parts: {unknown}')
            if not data.get('evidence_refs'):
                raise ValueError(
                    'assembly safety requires pinned fire evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assembly_id', 'assembly_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'FinishAssemblySafetyEvidence':
        return _seal(
            cls, payload, 'assembly_id', 'assembly_sha256', 'fas'
        )


def evaluate_fire_eligibility_claim(
    evidence: ReactionToFireEvidence | None,
    assembly: FinishAssemblySafetyEvidence | None,
    *,
    acoustic_qualified: bool = False,
    required_standard: str | None = None,
) -> tuple[FireVerdict, str]:
    """Judge whether an acoustic treatment may be claimed fire-/
    finish-eligible (#648).
    """
    if evidence is None:
        if acoustic_qualified:
            return (
                'acoustic_not_safety',
                'acoustic suitability never implies fire eligibility',
            )
        return (
            'unclaimed_fire_evidence',
            'no pinned reaction-to-fire evidence',
        )
    if required_standard is not None and (
        evidence.test_standard != required_standard
    ):
        return (
            'test_scope_mismatch',
            f'{evidence.test_standard} is not {required_standard} — '
            'different measurand and assembly context',
        )
    if assembly is None:
        return (
            'lab_result_is_not_installation',
            'a laboratory result is not the installed assembly — '
            'eligibility is installation-specific',
        )
    return (
        'eligible_assembly',
        'pinned fire evidence bound to the declared assembly — '
        'jurisdictional approval remains external',
    )


FIRE_LABELS: dict[str, str] = {
    'eligible_assembly': '組立体として証拠あり',
    'lab_result_is_not_installation': '試験結果は設置状態ではない',
    'unclaimed_fire_evidence': '防火証拠なし',
    'acoustic_not_safety': '音響適合は防火適格ではない',
    'test_scope_mismatch': '試験規格不一致',
}
