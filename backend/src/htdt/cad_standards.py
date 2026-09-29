from __future__ import annotations

from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


STANDARDS_PROFILE_SCHEMA_VERSION = 1
STANDARDS_PROFILE_AUTHORITY_VERSION = 'standards-profile-1'
STANDARDS_EVALUATION_SCHEMA_VERSION = 1
STANDARDS_EVALUATION_AUTHORITY_VERSION = 'standards-evaluation-1'
STANDARDS_EVALUATOR_VERSION = 'standards-evaluator-1'
STANDARDS_SOURCE_AUTHORITY_ID_PREFIX = 'standards-source-authority'
STANDARDS_SOURCE_AUTHORITY_VERSION = '1'
STANDARDS_EXTRACTION_NORMALIZATION_VERSION = 'standards-extraction-1'

ComplianceStatus = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']
EvidenceBasis = Literal['predicted', 'measured']
EvidenceRequirement = Literal['none', 'predicted_or_measured', 'measured']
ComparisonOperator = Literal['min', 'max', 'range', 'equals']
AngleWrap = Literal['none', 'signed_180', 'unsigned_360']
ProfileKind = Literal['published', 'user_defined']
SourceContentKind = Literal['normative', 'guidance', 'policy_transform']
ObservedScalar = float | int | bool | str






def _criterion_digest(criterion: 'CriterionDefinition') -> str:
    return _digest(criterion.model_dump(mode='json'))


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    )


def _decimal(value: object) -> Decimal:
    if not _finite_number(value):
        raise ValueError('criterion numeric value must be finite')
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('criterion numeric value is invalid') from exc


class CriterionSource(BaseModel):
    """Citation plus, where claimed, an exact retained source-authority binding.

    ``publisher``/``document_title``/``document_version``/``reference`` remain the
    human-facing citation. ``authority_ref`` and ``extraction_id`` bind the source
    to one exact extraction record inside a retained ``StandardsSourceAuthority``;
    they must be supplied together so a claimed authority always names the exact
    extraction identity rather than a free-form document hint alone.
    """

    model_config = ConfigDict(frozen=True)

    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    content_kind: SourceContentKind | None = None
    authority_ref: ExactExternalAuthorityRef | None = None
    extraction_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_source(self) -> 'CriterionSource':
        if (self.authority_ref is None) != (self.extraction_id is None):
            raise ValueError(
                'criterion source authority ref and extraction id must be '
                'supplied together'
            )
        return self


class CriterionRule(BaseModel):
    """Explicit comparison semantics; no implicit tolerance is introduced."""

    model_config = ConfigDict(frozen=True)

    operator: ComparisonOperator
    minimum: float | None = None
    maximum: float | None = None
    expected: ObservedScalar | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    angle_wrap: AngleWrap = 'none'
    absolute_value: bool = False

    @field_validator('minimum', 'maximum')
    @classmethod
    def finite_bound(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(float(value)):
            raise ValueError('criterion bounds must be finite')
        return value

    @model_validator(mode='after')
    def valid_rule(self) -> 'CriterionRule':
        if self.operator == 'min':
            if self.minimum is None:
                raise ValueError('min rule requires minimum')
            if self.maximum is not None or self.expected is not None:
                raise ValueError('min rule accepts only minimum')
        elif self.operator == 'max':
            if self.maximum is None:
                raise ValueError('max rule requires maximum')
            if self.minimum is not None or self.expected is not None:
                raise ValueError('max rule accepts only maximum')
        elif self.operator == 'range':
            if self.minimum is None or self.maximum is None:
                raise ValueError('range rule requires minimum and maximum')
            if self.expected is not None:
                raise ValueError('range rule does not accept expected')
            if Decimal(str(self.minimum)) > Decimal(str(self.maximum)):
                raise ValueError('range minimum must not exceed maximum')
        else:
            if self.expected is None:
                raise ValueError('equals rule requires expected')
            if isinstance(self.expected, float) and not isfinite(self.expected):
                raise ValueError('equals rule expected value must be finite')
            if self.minimum is not None or self.maximum is not None:
                raise ValueError('equals rule does not accept numeric bounds')

        if self.angle_wrap != 'none' or self.absolute_value:
            if self.operator == 'equals' and not _finite_number(self.expected):
                raise ValueError('angle/absolute equality requires numeric expected value')
        if self.operator == 'equals' and _finite_number(self.expected):
            _decimal(self.expected)
        return self


class CriterionSourceExtraction(BaseModel):
    """One exact normalized extraction bound to a source-document location.

    ``rule``/``quantity``/``unit`` are the retained normalized structured data a
    published criterion must reproduce exactly; ``excerpt`` optionally retains a
    verbatim quote where licensing permits. ``content_kind`` distinguishes
    normative source text, guidance/recommendation, and HTDT/user policy
    transformation.
    """

    model_config = ConfigDict(frozen=True)

    extraction_id: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    content_kind: SourceContentKind
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    rule: CriterionRule
    excerpt: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)


class StandardsSourceAuthority(BaseModel):
    """Content-addressed source-document authority retained for published criteria.

    Where source bytes cannot be bundled, ``document_sha256``/``source_uri``
    record the verified external identity and ``extractions`` carry the exact
    field/rule provenance each published criterion must match. ``authority_id``
    and ``semantic_hash_sha256`` are derived from the semantic payload, so any
    change to the document identity or an extraction is a different authority.
    """

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(
        pattern=rf'^{STANDARDS_SOURCE_AUTHORITY_ID_PREFIX}:[0-9a-f]{{64}}$'
    )
    authority_version: Literal['1'] = STANDARDS_SOURCE_AUTHORITY_VERSION
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    document_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    normalization_version: str = Field(min_length=1)
    extractions: tuple[CriterionSourceExtraction, ...] = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_authority(self) -> 'StandardsSourceAuthority':
        extraction_ids = [item.extraction_id for item in self.extractions]
        if len(extraction_ids) != len(set(extraction_ids)):
            raise ValueError('source authority extraction ids must be unique')
        expected = _digest(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('StandardsSourceAuthority semantic hash mismatch')
        expected_id = f'{STANDARDS_SOURCE_AUTHORITY_ID_PREFIX}:{expected}'
        if self.authority_id != expected_id:
            raise ValueError('StandardsSourceAuthority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_hash_sha256'},
        )

    def ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )

    def extraction(self, extraction_id: str) -> CriterionSourceExtraction | None:
        return next(
            (
                item
                for item in self.extractions
                if item.extraction_id == extraction_id
            ),
            None,
        )


def build_standards_source_authority(
    *,
    publisher: str,
    document_title: str,
    document_version: str,
    extractions: Sequence[CriterionSourceExtraction],
    source_uri: str | None = None,
    document_sha256: str | None = None,
    normalization_version: str = STANDARDS_EXTRACTION_NORMALIZATION_VERSION,
    note: str | None = None,
) -> StandardsSourceAuthority:
    payload = {
        'authority_version': STANDARDS_SOURCE_AUTHORITY_VERSION,
        'publisher': publisher,
        'document_title': document_title,
        'document_version': document_version,
        'source_uri': source_uri,
        'document_sha256': document_sha256,
        'normalization_version': normalization_version,
        'extractions': [
            item.model_dump(mode='json') for item in extractions
        ],
        'note': note,
    }
    digest = _digest(payload)
    return StandardsSourceAuthority(
        authority_id=f'{STANDARDS_SOURCE_AUTHORITY_ID_PREFIX}:{digest}',
        publisher=publisher,
        document_title=document_title,
        document_version=document_version,
        source_uri=source_uri,
        document_sha256=document_sha256,
        normalization_version=normalization_version,
        extractions=tuple(extractions),
        note=note,
        semantic_hash_sha256=digest,
    )


class CriterionDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator(
        'applicable_domains',
        'required_inputs',
        'required_capabilities',
    )
    @classmethod
    def canonical_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    criterion_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source: CriterionSource
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    applicable_domains: tuple[str, ...]
    required_inputs: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    evidence_requirement: EvidenceRequirement = 'predicted_or_measured'
    rule: CriterionRule
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_definition(self) -> 'CriterionDefinition':
        if not self.applicable_domains:
            raise ValueError('criterion must declare at least one applicable domain')
        for label, values in (
            ('applicable domains', self.applicable_domains),
            ('required inputs', self.required_inputs),
            ('required capabilities', self.required_capabilities),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'criterion {label} must be unique')
            if any(not value for value in values):
                raise ValueError(f'criterion {label} must not contain empty values')
        return self


def validate_criterion_source_authority(
    criterion: CriterionDefinition,
    authority: StandardsSourceAuthority,
) -> CriterionSourceExtraction:
    """Verify a criterion is exactly backed by one retained source extraction.

    The claimed authority reference, citation identity, extraction identity, and
    the encoded quantity/unit/rule/content kind must all reproduce the retained
    authority exactly; anything else is fabricated or dangling provenance.
    """

    source = criterion.source
    ref = source.authority_ref
    if ref is None or source.extraction_id is None:
        raise ValueError('criterion does not carry an exact source authority')
    if (
        ref.authority_id != authority.authority_id
        or ref.authority_version != authority.authority_version
        or ref.semantic_hash_sha256 != authority.semantic_hash_sha256
    ):
        raise ValueError('criterion source authority reference mismatch')
    if (
        authority.publisher != source.publisher
        or authority.document_title != source.document_title
        or authority.document_version != source.document_version
    ):
        raise ValueError(
            'criterion source citation does not match the authority document'
        )
    extraction = authority.extraction(source.extraction_id)
    if extraction is None:
        raise ValueError('criterion source extraction is not in the authority')
    if extraction.reference != source.reference:
        raise ValueError(
            'criterion source reference does not match the extraction'
        )
    if source.content_kind is None:
        raise ValueError('criterion source must declare an explicit content kind')
    if extraction.content_kind != source.content_kind:
        raise ValueError(
            'criterion source content kind does not match the extraction'
        )
    if extraction.quantity != criterion.quantity:
        raise ValueError('criterion quantity is not backed by the extraction')
    if extraction.unit != criterion.unit:
        raise ValueError('criterion unit is not backed by the extraction')
    if extraction.rule != criterion.rule:
        raise ValueError('criterion rule is not backed by the extraction')
    return extraction


class StandardsProfile(BaseModel):
    """Immutable profile data. Physical truth and optimization score remain external."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = STANDARDS_PROFILE_SCHEMA_VERSION
    authority_version: Literal['standards-profile-1'] = STANDARDS_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    profile_kind: ProfileKind
    criteria: tuple[CriterionDefinition, ...]
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'StandardsProfile':
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError('StandardsProfile criterion ids must be unique')
        if self.profile_semantic_hash != _digest(self.semantic_payload()):
            raise ValueError('StandardsProfile semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'version': self.version,
            'name': self.name,
            'profile_kind': self.profile_kind,
            'criteria': [
                criterion.model_dump(mode='json')
                for criterion in self.criteria
            ],
        }


def build_standards_profile(
    *,
    profile_id: str,
    version: str,
    name: str,
    profile_kind: ProfileKind,
    criteria: Sequence[CriterionDefinition],
) -> StandardsProfile:
    items = tuple(criteria)
    payload = {
        'schema_version': STANDARDS_PROFILE_SCHEMA_VERSION,
        'authority_version': STANDARDS_PROFILE_AUTHORITY_VERSION,
        'profile_id': profile_id,
        'version': version,
        'name': name,
        'profile_kind': profile_kind,
        'criteria': [item.model_dump(mode='json') for item in items],
    }
    return StandardsProfile(
        profile_id=profile_id,
        version=version,
        name=name,
        profile_kind=profile_kind,
        criteria=items,
        profile_semantic_hash=_digest(payload),
    )


def build_user_standards_profile(
    *,
    profile_id: str,
    version: str,
    name: str,
    criteria: Sequence[CriterionDefinition],
) -> StandardsProfile:
    return build_standards_profile(
        profile_id=profile_id,
        version=version,
        name=name,
        profile_kind='user_defined',
        criteria=criteria,
    )


class StandardsEvaluationTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator('entity_ids', 'applicable_domains')
    @classmethod
    def canonical_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    entity_ids: tuple[str, ...] = ()
    applicable_domains: tuple[str, ...]

    @model_validator(mode='after')
    def valid_target(self) -> 'StandardsEvaluationTarget':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('SystemVariant id/hash must be supplied together')
        if not self.applicable_domains:
            raise ValueError('evaluation target must declare applicable domains')
        for label, values in (
            ('entity ids', self.entity_ids),
            ('applicable domains', self.applicable_domains),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'evaluation target {label} must be unique')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'entity_ids': sorted(self.entity_ids),
            'applicable_domains': sorted(self.applicable_domains),
        }


class CriterionEvidenceRef(BaseModel):
    """Typed reference to one exact criterion-observation evidence authority.

    ``kind`` selects the registered evidence resolver (for example
    ``standards_manual_observation``); ``evidence_id`` names the authority
    inside that source and ``evidence_sha256`` optionally pins its exact
    semantic hash. At the persistence boundary and on every authoritative
    read, each ref is re-resolved against the declared source: an id that
    cannot be resolved to an authority bound to the evaluation target is
    never evidence.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    detail: str | None = Field(default=None, min_length=1)


class CriterionObservation(BaseModel):
    """Exact observed/predicted scalar plus the inputs and capabilities that support it."""

    model_config = ConfigDict(frozen=True)

    @field_validator('entity_ids', 'provided_inputs', 'capabilities')
    @classmethod
    def canonical_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    @field_validator('evidence_refs')
    @classmethod
    def canonical_evidence_refs(
        cls,
        values: tuple[CriterionEvidenceRef, ...],
    ) -> tuple[CriterionEvidenceRef, ...]:
        return tuple(
            sorted(values, key=lambda item: (item.kind, item.evidence_id))
        )

    criterion_id: str = Field(min_length=1)
    entity_ids: tuple[str, ...] = ()
    observed_value: ObservedScalar | None = None
    unit: str | None = Field(default=None, min_length=1)
    evidence_basis: EvidenceBasis | None = None
    evidence_refs: tuple[CriterionEvidenceRef, ...] = ()
    provided_inputs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    applicable: bool | None = None

    @model_validator(mode='after')
    def valid_observation(self) -> 'CriterionObservation':
        for label, values in (
            ('entity ids', self.entity_ids),
            (
                'evidence refs',
                tuple(
                    (item.kind, item.evidence_id) for item in self.evidence_refs
                ),
            ),
            ('provided inputs', self.provided_inputs),
            ('capabilities', self.capabilities),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'observation {label} must be unique')
        if self.observed_value is not None and _finite_number(self.observed_value):
            _decimal(self.observed_value)
        if self.applicable is False:
            return self
        if self.evidence_basis is None and self.evidence_refs:
            raise ValueError('evidence refs require an explicit predicted/measured basis')
        return self


class CriterionEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    criterion_id: str = Field(min_length=1)
    criterion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_ids: tuple[str, ...] = ()
    status: ComplianceStatus
    observed_value: ObservedScalar | None = None
    unit: str | None = Field(default=None, min_length=1)
    evidence_basis: EvidenceBasis | None = None
    evidence_refs: tuple[CriterionEvidenceRef, ...] = ()
    reason_code: str = Field(min_length=1)
    missing_inputs: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()


class StandardsEvaluation(BaseModel):
    """Immutable criterion-by-criterion evidence. There is intentionally no total score."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = STANDARDS_EVALUATION_SCHEMA_VERSION
    authority_version: Literal['standards-evaluation-1'] = STANDARDS_EVALUATION_AUTHORITY_VERSION
    evaluator_version: Literal['standards-evaluator-1'] = STANDARDS_EVALUATOR_VERSION
    evaluation_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    target: StandardsEvaluationTarget
    observations: tuple[CriterionObservation, ...]
    results: tuple[CriterionEvaluationResult, ...]
    reevaluation_of_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'StandardsEvaluation':
        result_ids = [result.criterion_id for result in self.results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError('standards evaluation result ids must be unique')
        observation_ids = [item.criterion_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError('standards evaluation observation ids must be unique')
        input_hash = _digest(self.identity_payload())
        expected_id = f'standards-eval-{input_hash[:32]}'
        if self.evaluation_id != expected_id:
            raise ValueError('StandardsEvaluation deterministic identity mismatch')
        if self.evaluation_sha256 != _digest(self.semantic_payload()):
            raise ValueError('StandardsEvaluation semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        observations = sorted(
            (
                item.model_dump(mode='json')
                for item in self.observations
            ),
            key=lambda item: item['criterion_id'],
        )
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'evaluator_version': self.evaluator_version,
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'profile_semantic_hash': self.profile_semantic_hash,
            'target': self.target.identity_payload(),
            'observations': observations,
            'reevaluation_of_id': self.reevaluation_of_id,
        }

    def semantic_payload(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            'evaluation_id': self.evaluation_id,
            'results': [item.model_dump(mode='json') for item in self.results],
        }


class StandardsHardConstraintGate(BaseModel):
    """Explicit downstream gate. Only selected criteria can block a candidate."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    selected_criterion_ids: tuple[str, ...]
    blocking_criterion_ids: tuple[str, ...]
    allowed: bool

    @model_validator(mode='after')
    def valid_gate(self) -> 'StandardsHardConstraintGate':
        if not set(self.blocking_criterion_ids).issubset(self.selected_criterion_ids):
            raise ValueError('blocking criteria must be explicitly selected')
        if self.allowed != (not self.blocking_criterion_ids):
            raise ValueError('hard constraint gate allowed flag is inconsistent')
        return self


def _domain_applies(
    criterion: CriterionDefinition,
    target: StandardsEvaluationTarget,
) -> bool:
    criterion_domains = set(criterion.applicable_domains)
    target_domains = set(target.applicable_domains)
    return (
        '*' in criterion_domains
        or '*' in target_domains
        or bool(criterion_domains.intersection(target_domains))
    )


def _normalize_numeric(value: ObservedScalar, rule: CriterionRule) -> Decimal:
    number = _decimal(value)
    if rule.angle_wrap == 'unsigned_360':
        full = Decimal('360')
        number %= full
        if number < 0:
            number += full
    elif rule.angle_wrap == 'signed_180':
        full = Decimal('360')
        half = Decimal('180')
        number = (number + half) % full
        if number < 0:
            number += full
        number -= half
    if rule.absolute_value:
        number = abs(number)
    return number


def _compare(observed: ObservedScalar, rule: CriterionRule) -> bool:
    if rule.operator == 'equals':
        expected = rule.expected
        if _finite_number(expected):
            return _normalize_numeric(observed, rule) == _decimal(expected)
        return observed == expected

    value = _normalize_numeric(observed, rule)
    if rule.operator in {'min', 'range'}:
        assert rule.minimum is not None
        minimum = Decimal(str(rule.minimum))
        lower_ok = value >= minimum if rule.lower_inclusive else value > minimum
        if not lower_ok:
            return False
    if rule.operator in {'max', 'range'}:
        assert rule.maximum is not None
        maximum = Decimal(str(rule.maximum))
        upper_ok = value <= maximum if rule.upper_inclusive else value < maximum
        if not upper_ok:
            return False
    return True


def _result(
    criterion: CriterionDefinition,
    *,
    status: ComplianceStatus,
    observation: CriterionObservation | None,
    reason_code: str,
    missing_inputs: Sequence[str] = (),
    missing_capabilities: Sequence[str] = (),
) -> CriterionEvaluationResult:
    return CriterionEvaluationResult(
        criterion_id=criterion.criterion_id,
        criterion_sha256=_criterion_digest(criterion),
        entity_ids=() if observation is None else observation.entity_ids,
        status=status,
        observed_value=None if observation is None else observation.observed_value,
        unit=None if observation is None else observation.unit,
        evidence_basis=None if observation is None else observation.evidence_basis,
        evidence_refs=() if observation is None else observation.evidence_refs,
        reason_code=reason_code,
        missing_inputs=tuple(sorted(missing_inputs)),
        missing_capabilities=tuple(sorted(missing_capabilities)),
    )


def evaluate_standards_profile(
    *,
    profile: StandardsProfile,
    target: StandardsEvaluationTarget,
    observations: Sequence[CriterionObservation],
    created_at_utc: str,
    reevaluation_of_id: str | None = None,
) -> StandardsEvaluation:
    """Evaluate exact observations without deriving physical quantities or optimization scores."""

    observation_items = tuple(observations)
    observation_by_id = {item.criterion_id: item for item in observation_items}
    if len(observation_by_id) != len(observation_items):
        raise ValueError('criterion observations must have unique criterion ids')

    profile_ids = {criterion.criterion_id for criterion in profile.criteria}
    unknown_observations = set(observation_by_id) - profile_ids
    if unknown_observations:
        raise ValueError(
            'observation references criterion outside profile: '
            f'{sorted(unknown_observations)}'
        )

    target_entity_ids = set(target.entity_ids)
    for observation in observation_items:
        unknown_entities = set(observation.entity_ids) - target_entity_ids
        if unknown_entities:
            raise ValueError(
                'criterion observation entity binding is outside evaluation target: '
                f'{sorted(unknown_entities)}'
            )

    results: list[CriterionEvaluationResult] = []
    for criterion in profile.criteria:
        observation = observation_by_id.get(criterion.criterion_id)

        if not _domain_applies(criterion, target):
            results.append(_result(
                criterion,
                status='NOT_APPLICABLE',
                observation=observation,
                reason_code='domain_not_applicable',
            ))
            continue

        if observation is not None and observation.applicable is False:
            results.append(_result(
                criterion,
                status='NOT_APPLICABLE',
                observation=observation,
                reason_code='explicit_not_applicable',
            ))
            continue

        if observation is None:
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=None,
                reason_code='missing_observation',
            ))
            continue

        if observation.unit != criterion.unit:
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=observation,
                reason_code='unit_mismatch',
            ))
            continue

        missing_inputs = (
            set(criterion.required_inputs)
            - set(observation.provided_inputs)
        )
        missing_capabilities = (
            set(criterion.required_capabilities)
            - set(observation.capabilities)
        )
        if missing_inputs or missing_capabilities:
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=observation,
                reason_code='missing_input_or_capability',
                missing_inputs=missing_inputs,
                missing_capabilities=missing_capabilities,
            ))
            continue

        if criterion.evidence_requirement != 'none':
            if not observation.evidence_refs or observation.evidence_basis is None:
                results.append(_result(
                    criterion,
                    status='UNKNOWN',
                    observation=observation,
                    reason_code='missing_evidence',
                ))
                continue
        if (
            criterion.evidence_requirement == 'measured'
            and observation.evidence_basis != 'measured'
        ):
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=observation,
                reason_code='measurement_evidence_required',
            ))
            continue

        if observation.observed_value is None:
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=observation,
                reason_code='missing_observed_value',
            ))
            continue

        try:
            passed = _compare(observation.observed_value, criterion.rule)
        except (TypeError, ValueError):
            results.append(_result(
                criterion,
                status='UNKNOWN',
                observation=observation,
                reason_code='invalid_observed_value',
            ))
            continue

        results.append(_result(
            criterion,
            status='PASS' if passed else 'FAIL',
            observation=observation,
            reason_code='comparison_pass' if passed else 'comparison_fail',
        ))

    normalized_observations = tuple(
        sorted(observation_items, key=lambda item: item.criterion_id)
    )
    identity_payload = {
        'schema_version': STANDARDS_EVALUATION_SCHEMA_VERSION,
        'authority_version': STANDARDS_EVALUATION_AUTHORITY_VERSION,
        'evaluator_version': STANDARDS_EVALUATOR_VERSION,
        'profile_id': profile.profile_id,
        'profile_version': profile.version,
        'profile_semantic_hash': profile.profile_semantic_hash,
        'target': target.identity_payload(),
        'observations': [
            item.model_dump(mode='json')
            for item in normalized_observations
        ],
        'reevaluation_of_id': reevaluation_of_id,
    }
    input_hash = _digest(identity_payload)
    evaluation_id = f'standards-eval-{input_hash[:32]}'
    semantic_payload = {
        **identity_payload,
        'evaluation_id': evaluation_id,
        'results': [item.model_dump(mode='json') for item in results],
    }
    return StandardsEvaluation(
        evaluation_id=evaluation_id,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_semantic_hash=profile.profile_semantic_hash,
        target=target,
        observations=normalized_observations,
        results=tuple(results),
        reevaluation_of_id=reevaluation_of_id,
        created_at_utc=created_at_utc,
        evaluation_sha256=_digest(semantic_payload),
    )


def reevaluate_standards_profile(
    *,
    previous: StandardsEvaluation,
    profile: StandardsProfile,
    observations: Sequence[CriterionObservation],
    created_at_utc: str,
) -> StandardsEvaluation:
    """Create a new immutable evaluation explicitly linked to a historical result."""

    return evaluate_standards_profile(
        profile=profile,
        target=previous.target,
        observations=observations,
        created_at_utc=created_at_utc,
        reevaluation_of_id=previous.evaluation_id,
    )


def explicit_hard_constraint_gate(
    evaluation: StandardsEvaluation,
    *,
    selected_criterion_ids: Sequence[str],
) -> StandardsHardConstraintGate:
    """Apply an explicit downstream policy without changing evaluation identity."""

    result_ids = {result.criterion_id for result in evaluation.results}
    selected = tuple(sorted(set(selected_criterion_ids)))
    unknown = set(selected) - result_ids
    if unknown:
        raise ValueError(
            'hard constraint references criterion outside evaluation: '
            f'{sorted(unknown)}'
        )
    status_by_id = {
        result.criterion_id: result.status
        for result in evaluation.results
    }
    blocking = tuple(
        criterion_id
        for criterion_id in selected
        if status_by_id[criterion_id] in {'FAIL', 'UNKNOWN'}
    )
    return StandardsHardConstraintGate(
        evaluation_id=evaluation.evaluation_id,
        selected_criterion_ids=selected,
        blocking_criterion_ids=blocking,
        allowed=not blocking,
    )
