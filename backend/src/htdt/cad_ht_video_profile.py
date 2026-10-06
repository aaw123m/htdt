"""CTA/CEDIA-CEB23-B home-theater video-design profile crosswalk
(issue #741).

CEB23-B (Home Theater Video Design) is a published residential
recommended-practice resource covering screen size vs seating,
resolution/viewing distance, image angular extent, masking when image
shape differs from screen shape, room lighting and room surface
colors. HTDT must map requirements to its own evidence authorities
without reproducing protected standard text — the profile declares
exact document/revision identity plus a requirement-to-authority
crosswalk; unsupported/unknown requirements stay honest.

Basis: CTA/CEDIA-CEB23-B public description (CTA); the same
external-profile orchestration as RP22/RP32 (#579/#585).
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


# CEB23-B coverage areas per CTA's public description — the profile
# maps each to an HTDT authority kind without copying the text.
_REQUIREMENT_KEYS = (
    'screen_size_vs_seating',
    'resolution_viewing_distance',
    'image_angular_extent',
    'masking_shape_mismatch',
    'room_lighting',
    'room_surface_color',
    'other',
)
_EVIDENCE_CLASSES = (
    'as_built_verified', 'design_prediction', 'declared_only',
    'unmapped', 'unknown',
)

RequirementVerdict = Literal[
    'as_built_satisfied',
    'design_satisfied',
    'unverified',
    'unmapped_requirement',
    'profile_unsupported',
]


class HomeTheaterVideoDesignProfile(BaseModel):
    """Versioned CEB23-B crosswalk profile (#741).

    The profile never embeds recommended-practice text — it pins the
    external document identity (standard + edition, e.g. via #599
    standards registry) and a mapping from each requirement key to the
    HTDT authority kind that may verify it.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    standard_ref: AuthorityRef
    edition: str
    # requirement_key -> HTDT authority kind expected to verify it
    # (e.g. 'viewing_envelope', 'image_geometry', 'viewing_environment')
    requirement_map: dict[str, str]

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('standard_ref') is None:
                raise ValueError(
                    'a CEB23 profile requires a pinned standard '
                    'document reference'
                )
            if not data.get('edition'):
                raise ValueError(
                    'a CEB23 profile requires an exact edition '
                    'identifier'
                )
            rmap = data.get('requirement_map') or {}
            unknown = [k for k in rmap if k not in _REQUIREMENT_KEYS]
            if unknown:
                raise ValueError(
                    f'unknown CEB23 requirement keys: {unknown}'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'HomeTheaterVideoDesignProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'htvdp'
        )


class CEB23Evaluation(BaseModel):
    """One CEB23-B evaluation against a pinned profile (#741).

    Per-requirement results keep their evidence class — design
    predictions never masquerade as as-built verification.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    # requirement_key -> evidence class observed
    requirement_evidence: dict[str, str]
    requirement_refs: dict[str, AuthorityRef] | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'a CEB23 evaluation requires a pinned profile'
                )
            ev = data.get('requirement_evidence') or {}
            if not ev:
                raise ValueError(
                    'an evaluation without requirement evidence is '
                    'not evidence'
                )
            for key, cls_ in ev.items():
                if key not in _REQUIREMENT_KEYS:
                    raise ValueError(
                        f'unknown CEB23 requirement key: {key}'
                    )
                if cls_ not in _EVIDENCE_CLASSES:
                    raise ValueError(
                        f'unknown evidence class: {cls_}'
                    )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'evaluation_id', 'evaluation_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'CEB23Evaluation':
        return _seal(
            cls, payload, 'evaluation_id', 'evaluation_sha256', 'ceb23'
        )


def evaluate_ceb23_requirement(
    profile: HomeTheaterVideoDesignProfile | None,
    evaluation: CEB23Evaluation | None,
    requirement_key: str,
) -> tuple[RequirementVerdict, str]:
    """Judge one CEB23 requirement (#741).

    - profile missing or key unmapped → 'profile_unsupported' /
      'unmapped_requirement'
    - evaluation missing the key → 'unverified'
    - 'as_built_verified' → 'as_built_satisfied'; 'design_prediction'
      → 'design_satisfied' (never upgraded silently)
    """
    if requirement_key not in _REQUIREMENT_KEYS:
        return (
            'profile_unsupported',
            f'{requirement_key} is outside the CEB23 coverage map',
        )
    if profile is None:
        return 'profile_unsupported', 'no CEB23 profile pinned'
    if requirement_key not in profile.requirement_map:
        return (
            'unmapped_requirement',
            f'{requirement_key} has no HTDT authority mapping — cannot '
            'claim coverage',
        )
    if evaluation is None:
        return 'unverified', 'no evaluation evidence pinned'
    cls_ = evaluation.requirement_evidence.get(requirement_key)
    if cls_ in (None, 'unmapped', 'unknown'):
        return 'unverified', f'{requirement_key} evidence is {cls_}'
    if cls_ == 'as_built_verified':
        return 'as_built_satisfied', 'as-built evidence verified'
    if cls_ == 'declared_only':
        return 'unverified', 'declaration only — not verified'
    return (
        'design_satisfied',
        'design prediction only — as-built verification pending',
    )


CEB23_LABELS: dict[str, str] = {
    'as_built_satisfied': '実測で適合',
    'design_satisfied': '設計予測で適合（実測未検証）',
    'unverified': '未検証',
    'unmapped_requirement': '要件↔権威マッピングなし',
    'profile_unsupported': 'プロファイル外の要件',
}
