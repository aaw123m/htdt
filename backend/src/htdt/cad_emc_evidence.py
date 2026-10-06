"""AV/ICT product EMC evidence authority (issue #752).

A product-safety approval does NOT establish an EMC emissions/immunity
profile, and a field hum/dropout symptom does NOT by itself prove
product EMC non-conformance. EMC evidence must pin the test/certification
profile per standard (emissions AND immunity separately), bound to the
product identity and configuration — type-test results are evidence
about a tested configuration, not blanket field guarantees.

Basis: issue #752 scope; CISPR 32/EN 55032 (multimedia emissions),
CISPR 35/EN 55035 (multimedia immunity); #606 field EMI diagnosis;
#751 product safety; #667 production BOM.
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


EmcDomain = Literal['emissions', 'immunity']

EMC_LABELS: dict[str, str] = {
    'emc_evidence_complete': 'EMC証拠は完備',
    'emc_partial_evidence': 'EMC証拠は部分的',
    'safety_is_not_emc': '安全性承認はEMC証拠ではない',
    'symptom_not_nonconformance': '症状だけでは不適合を立証できない',
    'insufficient_evidence': '証拠不足',
}


class EmcProductProfile(BaseModel):
    """EMC test/certification profile for a product configuration
    (emc- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    product_ref: AuthorityRef
    emissions_standard: str | None = None
    emissions_test_ref: AuthorityRef | None = None
    immunity_standard: str | None = None
    immunity_test_ref: AuthorityRef | None = None
    tested_configuration: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'EmcProductProfile':
        _require_refs(self.product_ref)
        if self.emissions_standard is not None \
                and self.emissions_test_ref is None:
            raise ValueError(
                'declared emissions standard needs a pinned test ref')
        if self.immunity_standard is not None \
                and self.immunity_test_ref is None:
            raise ValueError(
                'declared immunity standard needs a pinned test ref')
        if self.emissions_test_ref is None \
                and self.immunity_test_ref is None:
            raise ValueError(
                'EMC profile needs at least one pinned test ref')
        for ref in (self.emissions_test_ref, self.immunity_test_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'EmcProductProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'emc')


class EmcSymptomRecord(BaseModel):
    """Field EMI symptom bound to a diagnosis path, NOT a
    conformance verdict (emcs- prefix)."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    product_ref: AuthorityRef
    symptom: str
    diagnosis_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'EmcSymptomRecord':
        _require_refs(self.product_ref)
        if not self.symptom:
            raise ValueError('symptom must be declared')
        if self.diagnosis_ref is not None:
            _require_refs(self.diagnosis_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'EmcSymptomRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'emcs')


def evaluate_emc_claim(
    profile: EmcProductProfile | None,
    has_safety_approval: bool = False,
    symptom: EmcSymptomRecord | None = None,
) -> tuple[str, str]:
    """Safety approval never substitutes for EMC evidence; a field
    symptom alone never proves non-conformance."""
    if profile is None:
        if has_safety_approval:
            return ('safety_is_not_emc', 'approval_not_emc_profile')
        if symptom is not None:
            return ('symptom_not_nonconformance',
                    'field_symptom_needs_diagnosis')
        return ('insufficient_evidence', 'no_emc_profile')
    missing = []
    if profile.emissions_test_ref is None:
        missing.append('emissions')
    if profile.immunity_test_ref is None:
        missing.append('immunity')
    if missing:
        return ('emc_partial_evidence', 'missing_' + '_'.join(missing))
    return ('emc_evidence_complete', 'both_domains_pinned')
