"""External validation dataset registry and evidence program (#793).

Turns "we compared against an external source once" into a governed
evidence program: which external datasets exist, what class of evidence
each one provides, and where the gaps are.

- ``ExternalValidationDatasetRecord`` binds each dataset's identity,
  origin (measured / simulated / authored), the E1–E6 evidence class it
  can support, its provenance/licensing, and whether it is a locked
  holdout (never used to tune anything, only to check).
- ``ValidationEvidenceProgram`` evaluates coverage across the registry:
  which required evidence classes are populated by which datasets,
  which are EMPTY, and whether measured-vs-simulated separation is
  preserved (a simulated dataset never satisfies a measured-evidence
  requirement).
- Locked-holdout and fresh-campaign rules are first-class fields:
  a holdout dataset is declared before evaluation, and a record marks
  whether a fresh measurement campaign is still required.

Evidence classes (per issue #793):
  E1  analytic/reference closed-form checks
  E2  measured-room ground truth
  E3  device/system qualification data
  E4  curated third-party datasets
  E5  synthetic/simulated references
  E6  negative/adversarial cases
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ValidationEvidenceClass = Literal['E1', 'E2', 'E3', 'E4', 'E5', 'E6']

DatasetOrigin = Literal['measured', 'simulated', 'authored', 'mixed']

DatasetLicense = Literal[
    'internal_owned',
    'public_dataset',
    'licensed_commercial',
    'restricted_attribution',
    'unknown',
]

HoldoutRole = Literal['holdout', 'development', 'reference', 'calibration']

EvidenceCoverageStatus = Literal[
    'COVERED',
    'PARTIAL',
    'EMPTY',
    'BLOCKED_BY_LICENSE',
    'NEEDS_FRESH_CAMPAIGN',
]

VALIDATION_SCHEMA_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class ExternalValidationDatasetRecord(BaseModel):
    """One external dataset usable as validation evidence (#793 §2).

    Origin is explicit: a ``measured`` record describes physically
    acquired ground truth; ``simulated`` records are model-generated and
    never satisfy measured-evidence requirements."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    dataset_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    origin: DatasetOrigin
    evidence_classes: tuple[ValidationEvidenceClass, ...] = Field(
        min_length=1
    )
    license_kind: DatasetLicense
    holdout_role: HoldoutRole
    provenance_note: str = Field(min_length=1)
    source_reference: str | None = None
    content_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    acquired_at_utc: str | None = None
    locked: bool = False
    fresh_campaign_required: bool = False
    fresh_campaign_reason: str | None = None
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_dataset(self) -> 'ExternalValidationDatasetRecord':
        if not self.dataset_id.startswith('validation-dataset:'):
            raise ValueError(
                'dataset id must use validation-dataset: prefix'
            )
        if self.locked and self.holdout_role != 'holdout':
            raise ValueError('only holdout datasets may be locked')
        if self.fresh_campaign_required and not self.fresh_campaign_reason:
            raise ValueError(
                'fresh_campaign_required needs a reason'
            )
        if self.origin == 'measured' and 'E2' not in self.evidence_classes:
            raise ValueError('measured origin must offer E2 evidence')
        if self.origin == 'simulated' and 'E2' in self.evidence_classes:
            raise ValueError(
                'simulated datasets never provide measured (E2) evidence'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('validation dataset hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'dataset_id': self.dataset_id,
            'title': self.title,
            'origin': self.origin,
            'evidence_classes': list(self.evidence_classes),
            'license_kind': self.license_kind,
            'holdout_role': self.holdout_role,
            'provenance_note': self.provenance_note,
            'source_reference': self.source_reference,
            'content_sha256': self.content_sha256,
            'acquired_at_utc': self.acquired_at_utc,
            'locked': self.locked,
            'fresh_campaign_required': self.fresh_campaign_required,
            'fresh_campaign_reason': self.fresh_campaign_reason,
        }


class EvidenceClassCoverage(BaseModel):
    """Coverage status for one evidence class."""

    model_config = ConfigDict(frozen=True)

    evidence_class: ValidationEvidenceClass
    status: EvidenceCoverageStatus
    datasets: tuple[str, ...] = ()
    measured_datasets: tuple[str, ...] = ()
    simulated_datasets: tuple[str, ...] = ()
    gap_note: str = ''


class ValidationEvidenceProgram(BaseModel):
    """Derived coverage report over the registry — per evidence class
    status plus measured/simulated separation (#793 §4-5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    program_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    rows: tuple[EvidenceClassCoverage, ...]
    locked_holdout_ids: tuple[str, ...] = ()
    fresh_campaign_ids: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_program(self) -> 'ValidationEvidenceProgram':
        if not self.program_id.startswith('validation-program:'):
            raise ValueError(
                'program id must use validation-program: prefix'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('evidence program hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'program_id': self.program_id,
            'created_at_utc': self.created_at_utc,
            'rows': [row.model_dump(mode='json') for row in self.rows],
            'locked_holdout_ids': list(self.locked_holdout_ids),
            'fresh_campaign_ids': list(self.fresh_campaign_ids),
        }


def evaluate_evidence_program(
    datasets: tuple[ExternalValidationDatasetRecord, ...],
    *,
    program_id: str,
    created_at_utc: str,
) -> ValidationEvidenceProgram:
    """Evaluate evidence-class coverage across the dataset registry."""
    rows: list[EvidenceClassCoverage] = []
    for eclass in ('E1', 'E2', 'E3', 'E4', 'E5', 'E6'):
        providers = [
            d for d in datasets if eclass in d.evidence_classes
        ]
        measured = [d.dataset_id for d in providers if d.origin == 'measured']
        simulated = [
            d.dataset_id for d in providers if d.origin == 'simulated'
        ]
        licensed_blocked = [
            d.dataset_id
            for d in providers
            if d.license_kind in ('licensed_commercial', 'unknown')
        ]
        needs_fresh = [
            d.dataset_id for d in providers if d.fresh_campaign_required
        ]
        if not providers:
            status: EvidenceCoverageStatus = 'EMPTY'
            gap = 'no dataset registered for this evidence class'
        elif needs_fresh and len(needs_fresh) == len(providers):
            status = 'NEEDS_FRESH_CAMPAIGN'
            gap = 'all registered datasets need a fresh campaign'
        elif licensed_blocked and len(licensed_blocked) == len(providers):
            status = 'BLOCKED_BY_LICENSE'
            gap = 'all registered datasets are license-blocked'
        elif len(providers) == 1 or licensed_blocked or needs_fresh:
            status = 'PARTIAL'
            gap = ''
        else:
            status = 'COVERED'
            gap = ''
        if eclass == 'E2' and providers and not measured:
            status = 'EMPTY' if not providers else 'PARTIAL'
            gap = 'no measured ground-truth dataset registered'
        rows.append(
            EvidenceClassCoverage(
                evidence_class=eclass,
                status=status,
                datasets=tuple(d.dataset_id for d in providers),
                measured_datasets=tuple(measured),
                simulated_datasets=tuple(simulated),
                gap_note=gap,
            )
        )
    locked = tuple(
        d.dataset_id for d in datasets if d.locked and d.holdout_role == 'holdout'
    )
    fresh = tuple(
        d.dataset_id for d in datasets if d.fresh_campaign_required
    )
    payload: dict[str, Any] = {
        'program_id': program_id,
        'created_at_utc': created_at_utc,
        'rows': tuple(rows),
        'locked_holdout_ids': locked,
        'fresh_campaign_ids': fresh,
    }
    provisional = ValidationEvidenceProgram.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ValidationEvidenceProgram.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


__all__ = [
    'DatasetLicense',
    'DatasetOrigin',
    'EvidenceClassCoverage',
    'EvidenceCoverageStatus',
    'ExternalValidationDatasetRecord',
    'HoldoutRole',
    'VALIDATION_SCHEMA_VERSION',
    'ValidationEvidenceClass',
    'ValidationEvidenceProgram',
    'evaluate_evidence_program',
]
