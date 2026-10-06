"""Product-safety and EMC compliance evidence authority (issues
#751, #752).

A device can be electrically/performance-compatible while its
product-safety certification scope for the project market is
unknown (#751) — HTDT records the certification/listing identity and
scope, it does NOT reproduce safety testing. And a product-safety
approval does not establish an EMC emissions/immunity profile
(#752) — CISPR 32/35 class evidence is a separate authority; a
field hum symptom does not itself prove product EMC
non-conformance.

Basis: IEC 62368-1 + national adoptions (#751); CISPR 32:2015+AMD1
Class A/B emissions and CISPR 35 immunity (#752).
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


class ProductSafetyEvidence(BaseModel):
    """External product-safety certification identity (#751) —
    standard/edition, listing body, exact model/variant scope and
    certificate validity. 'unknown' standard fails closed."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    safety_standard: Literal[
        'iec_62368_1', 'iec_60065', 'ul_listed', 'ce_lvd',
        'other', 'unknown',
    ]
    standard_edition: str | None = None
    listing_body: str | None = None
    tested_variant: str
    certificate_ref: AuthorityRef | None = None
    market_scope: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('safety_standard') not in (
                'iec_62368_1', 'iec_60065', 'ul_listed', 'ce_lvd',
                'other', 'unknown',
            ):
                raise ValueError('unknown safety standard')
            if data.get('safety_standard') == 'unknown':
                raise ValueError(
                    'safety evidence must declare its standard'
                )
            if not data.get('tested_variant'):
                raise ValueError(
                    'safety evidence requires the exact tested '
                    'model/variant'
                )
            if data.get('certificate_ref') is None:
                raise ValueError(
                    'safety evidence requires the certificate/'
                    'listing identity'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ProductSafetyEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'pse'
        )


class EMCComplianceEvidence(BaseModel):
    """EMC emissions/immunity profile (#752) — CISPR 32/35 class and
    revision identity bound to tested configuration; distinct from
    product safety AND from field-installed environment."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_kind: Literal[
        'cispr_32_emissions', 'cispr_35_immunity', 'fcc_part15',
        'other', 'unknown',
    ]
    equipment_class: Literal['class_a', 'class_b', 'other'] | None = None
    tested_configuration: str
    report_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_kind') not in (
                'cispr_32_emissions', 'cispr_35_immunity',
                'fcc_part15', 'other', 'unknown',
            ):
                raise ValueError('unknown EMC profile kind')
            if data.get('profile_kind') == 'unknown':
                raise ValueError(
                    'EMC evidence must declare its profile'
                )
            if data.get('profile_kind') == 'cispr_32_emissions' and (
                data.get('equipment_class') is None
            ):
                raise ValueError(
                    'CISPR 32 emissions require the Class A/B '
                    'declaration'
                )
            if not data.get('tested_configuration'):
                raise ValueError(
                    'EMC evidence requires the tested '
                    'product/configuration'
                )
            if data.get('report_ref') is None:
                raise ValueError(
                    'EMC evidence requires the test report identity'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'EMCComplianceEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'emc'
        )


ComplianceVerdict = Literal[
    'qualified_compliance',
    'safety_unverified',
    'emc_unverified',
    'performance_is_not_safety',
    'field_symptom_is_not_emc',
    'scope_mismatch',
]


def evaluate_compliance_claim(
    safety: ProductSafetyEvidence | None,
    emc: EMCComplianceEvidence | None,
    *,
    performance_qualified: bool = False,
    field_symptom_observed: bool = False,
    project_market: str | None = None,
) -> tuple[ComplianceVerdict, str]:
    """Judge a product-compliance claim (#751/#752)."""
    if safety is None:
        if performance_qualified:
            return (
                'performance_is_not_safety',
                'electrical/performance compatibility does not prove '
                'product-safety certification',
            )
        return ('safety_unverified', 'no safety evidence pinned')
    if (
        project_market is not None
        and safety.market_scope is not None
        and project_market.lower() not in safety.market_scope.lower()
    ):
        return (
            'scope_mismatch',
            'certification scope does not cover the project market',
        )
    if emc is None:
        return (
            'emc_unverified',
            'safety evidence exists but EMC profile unverified — '
            'safety approval does not establish EMC',
        )
    if field_symptom_observed:
        return (
            'field_symptom_is_not_emc',
            'a field symptom may indicate installation environment — '
            'it does not alone prove product EMC non-conformance',
        )
    return (
        'qualified_compliance',
        'safety and EMC evidence pinned to tested configurations',
    )


COMPLIANCE_LABELS: dict[str, str] = {
    'qualified_compliance': '製品適合適格',
    'safety_unverified': '安全未検証',
    'emc_unverified': 'EMC未検証',
    'performance_is_not_safety': '性能適合は安全ではない',
    'field_symptom_is_not_emc': '現地症状はEMC証拠ではない',
    'scope_mismatch': '認証範囲不一致',
}
