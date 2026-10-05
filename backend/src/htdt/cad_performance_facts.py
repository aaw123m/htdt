"""CEDIA RP1 Performance Facts ingestion authority (issue #586).

A provenance-safe ingestion layer for manufacturer engineering data —
the emerging CEDIA RP1 "Performance Facts" format and its neighbours.
The layer is *source/revision driven*: it never hard-codes a guessed
final RP1 schema. Each import is bound to an exact
:class:`PerformanceFactsProfile` (publisher, family, revision, maturity
state, field-mapping version) so a review-draft field set is never
labelled as a published recommended practice.

Records:

- :class:`PerformanceFactsProfile` — sealed external profile identity:
  publisher, family (RP1 / RP1-1 loudspeakers / future subprofile /
  other declared), exact document reference, revision/date, maturity
  state (industry_review / final_published / revised / withdrawn /
  superseded / unknown), source reference, field definitions and
  content hash. (issue #586 §1, §11)
- :class:`ManufacturerProductIdentity` — sealed product-instance
  identity: manufacturer, brand, model, hardware revision, firmware,
  variant, impedance version, region, serial only where the data are
  instance-specific. Two variants never merge on a similar marketing
  name. (§2)
- :class:`PerformanceFact` — quantity-first sealed fact: physical
  quantity kind, value kind + payload, declared unit, frequency/level
  domain, full :class:`PerformanceFactConditions`, evidence class and
  referenced test standards. Missing conditions stay missing; nothing
  estimates what was not supplied (§3, §4, §5, §6, §10).
- :class:`PerformanceFactsImport` — one sealed ingestion run: profile
  binding, extraction state (machine-readable / curated document /
  manual entry), source asset hash, unmapped and unsupported fields —
  both listed honestly rather than silently dropped. (§12)
- :class:`PerformanceFactsEvaluation` — sealed product-selection
  verdict (§8): ELIGIBLE / ELIGIBLE_WITH_LIMITATIONS /
  INSUFFICIENT_DATA / INCOMPATIBLE with enumerable reason codes.
  Missing data is never ranked as zero risk.
- :class:`PerformanceFactsProfileRebind` — sealed review→final
  transition record: exact from/to profiles, per-field dispositions,
  rebound imports; historical imports stay bound to the profile they
  were taken under (§11).

Contract properties:

- evidence classes are never promoted: ``rp1_profiled_manufacturer``
  and ``manufacturer_datasheet`` stay manufacturer-declared forever —
  only ``independent_lab`` / ``standardized_open_dataset`` /
  ``htdt_measured`` count as independent or measured evidence (§5);
- conflicting sources coexist per quantity/domain — reconciliation is
  a view, never an overwrite (§9);
- RP22 consumption is capped: imported manufacturer facts can feed a
  *design* estimate only, never commissioning proof (§7);
- no screen-scraped tables: source asset hash is recorded and unknown
  future fields are preserved verbatim (§12, §13).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

PERFORMANCE_FACTS_SCHEMA_VERSION = 1
PERFORMANCE_FACTS_AUTHORITY_VERSION = 'performance-facts-1'


#: Profile families (issue #586 §1). ``rp1_*`` entries name exact
#: published subprofiles only when confirmed by the source; the unknown
#: bucket never collapses to a published label.
PerformanceFactsFamily = Literal[
    'rp1',
    'rp1_1_loudspeakers',
    'rp1_2_amplifiers',
    'rp1_av_processors',
    'rp1_projectors',
    'rp1_screens',
    'other_declared',
]

PerformanceFactsMaturity = Literal[
    'industry_review',
    'final_published',
    'revised',
    'withdrawn',
    'superseded',
    'unknown',
]

PerformanceFactsEvidenceClass = Literal[
    'rp1_profiled_manufacturer_data',
    'manufacturer_datasheet',
    'independent_lab',
    'standardized_open_dataset',
    'htdt_measured',
    'user_entered',
    'derived',
    'unknown',
]

#: Which evidence classes count as independent (not manufacturer).
_INDEPENDENT_CLASSES = frozenset(
    {'independent_lab', 'standardized_open_dataset', 'htdt_measured'}
)

#: Quantity taxonomy (issue #586 §3): the physical quantity, never the
#: marketing label. ``other_declared`` keeps unanticipated RP1 fields
#: importable without inventing schema.
PerformanceQuantityKind = Literal[
    'frequency_response',
    'sensitivity',
    'reference_output',
    'directivity',
    'max_output',
    'compression',
    'distortion',
    'impedance',
    'power_handling',
    'thermal_limit',
    'amplifier_output',
    'dsp_capability',
    'channel_capability',
    'video_display',
    'projector_performance',
    'screen_optical',
    'screen_acoustic',
    'other_declared',
]

PerformanceFactValueKind = Literal[
    'scalar',
    'range',
    'curve_reference',
    'text',
    'unknown',
]

PerformanceEnvironment = Literal[
    'anechoic',
    'half_space',
    'in_room',
    'free_field',
    'other_declared',
    'unknown',
]

PerformanceDurationKind = Literal[
    'continuous',
    'burst',
    'peak_momentary',
    'declared',
    'unknown',
]

PerformanceExtractionState = Literal[
    'machine_readable',
    'curated_document',
    'manual_entry',
]

PerformanceSufficiency = Literal[
    'engineering_grade',
    'limited',
    'insufficient',
]

PerformanceSuitabilityVerdict = Literal[
    'eligible',
    'eligible_with_limitations',
    'insufficient_data',
    'incompatible',
]

PerformanceRequirementKind = Literal[
    'output_at_distance',
    'bandwidth',
    'amplifier_capability',
    'directivity_compatible',
    'physical_envelope',
    'generic',
]

PerformanceFieldDisposition = Literal[
    'unchanged',
    'renamed_equivalent',
    'redefined',
    'added',
    'removed',
    'suppressed',
]


# ---------------------------------------------------------------------------
# Small value models
# ---------------------------------------------------------------------------


def _require_finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


class PerformanceFactsFieldSpec(BaseModel):
    """One declared field of an external profile — carried by the profile
    record so the field set is never hard-coded (issue #586 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    field_name: str = Field(min_length=1)
    quantity_kind: PerformanceQuantityKind
    unit: str | None = None
    required_conditions: tuple[str, ...] = ()
    required: bool = True


class PerformanceTestMethodRef(BaseModel):
    """Referenced external test standard — exact publisher/number/
    revision/method, never normalized away (issue #586 §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    publisher: str = Field(min_length=1)
    standard_number: str = Field(min_length=1)
    revision: str | None = None
    clause_or_method: str | None = None
    note: str | None = None

    @property
    def display(self) -> str:
        parts = [self.publisher, self.standard_number]
        if self.revision:
            parts.append(self.revision)
        if self.clause_or_method:
            parts.append(self.clause_or_method)
        return ' '.join(parts)


class PerformanceFactConditions(BaseModel):
    """The test-condition authority for one fact (issue #586 §4).

    Every field is optional — but the *missing* state is what
    ``condition_gaps`` reports, never silently filled.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_domain: FrequencyDomain | None = None
    frequency_resolution_hz: float | None = None
    input_voltage_v: float | None = None
    input_power_w: float | None = None
    signal_kind: str | None = None
    measurement_distance_m: float | None = None
    environment: PerformanceEnvironment = 'unknown'
    axis_azimuth_deg: float | None = None
    axis_elevation_deg: float | None = None
    bandwidth_or_smoothing: str | None = None
    load_impedance_ohm: float | None = None
    channels_driven: int | None = None
    duration_kind: PerformanceDurationKind = 'unknown'
    duration_s: float | None = None
    temperature_c: float | None = None
    sample_rate_hz: float | None = None
    dsp_mode: str | None = None

    @field_validator(
        'frequency_resolution_hz',
        'input_voltage_v',
        'input_power_w',
        'measurement_distance_m',
        'axis_azimuth_deg',
        'axis_elevation_deg',
        'load_impedance_ohm',
        'duration_s',
        'temperature_c',
        'sample_rate_hz',
    )
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError('condition value must be finite')
        return value

    def missing(self, *condition_names: str) -> tuple[str, ...]:
        out = []
        for name in condition_names:
            field_value = getattr(self, name, None)
            if field_value is None or field_value == 'unknown':
                out.append(name)
        return tuple(out)


class PerformanceRequirement(BaseModel):
    """A product-selection requirement against product facts (§8)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: PerformanceRequirementKind
    params: dict[str, Any] = Field(default_factory=dict)
    description_ja: str = ''


class PerformanceFieldDispositionRecord(BaseModel):
    """How a field's semantics changed between profile revisions (§11)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    field_name: str = Field(min_length=1)
    disposition: PerformanceFieldDisposition
    detail: str | None = None


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class PerformanceFactsProfile(BaseModel):
    """Sealed external profile identity (#586 §1).

    ``maturity_state`` is load-bearing: ``industry_review`` never equals
    ``final_published``, and historical imports stay bound to the exact
    profile they were taken under (§11).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    family: PerformanceFactsFamily
    document_reference: str = Field(min_length=1)
    revision_or_date: str = Field(min_length=1)
    maturity_state: PerformanceFactsMaturity = 'unknown'
    source_reference: str = Field(min_length=1)
    source_sha256: str | None = None
    field_mapping_version: str = Field(min_length=1)
    field_definitions: tuple[PerformanceFactsFieldSpec, ...] = ()
    license_note: str = ''
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'PerformanceFactsProfile':
        expected = _digest(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('profile_sha256 does not match content')
        if self.profile_id != f'pfp:{expected}':
            raise ValueError('profile_id must be pfp:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('profile_id', None)
        payload.pop('profile_sha256', None)
        return payload

    def field_spec(self, field_name: str) -> PerformanceFactsFieldSpec | None:
        for spec in self.field_definitions:
            if spec.field_name == field_name:
                return spec
        return None


class ManufacturerProductIdentity(BaseModel):
    """Sealed product-instance identity (#586 §2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    product_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    brand: str | None = None
    model: str = Field(min_length=1)
    hardware_revision: str | None = None
    firmware: str | None = None
    variant: str | None = None
    region: str | None = None
    impedance_version: str | None = None
    serial_or_instance: str | None = None
    source_reference: str | None = None
    product_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'ManufacturerProductIdentity':
        expected = _digest(self.identity_payload())
        if self.product_sha256 != expected:
            raise ValueError('product_sha256 does not match content')
        if self.product_id != f'mfp:{expected}':
            raise ValueError('product_id must be mfp:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('product_id', None)
        payload.pop('product_sha256', None)
        return payload

    @property
    def instance_specific(self) -> bool:
        return self.serial_or_instance is not None


class PerformanceFact(BaseModel):
    """One quantity-first engineering fact (#586 §3, §4, §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    fact_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_sha256: str = Field(pattern=_SHA256_PATTERN)
    quantity_kind: PerformanceQuantityKind
    value_kind: PerformanceFactValueKind
    scalar_value: float | None = None
    unit: str | None = None
    range_min: float | None = None
    range_max: float | None = None
    text_value: str | None = None
    curve_reference: str | None = None
    domain: FrequencyDomain | None = None
    conditions: PerformanceFactConditions = Field(
        default_factory=PerformanceFactConditions
    )
    evidence_class: PerformanceFactsEvidenceClass = 'unknown'
    standard_refs: tuple[PerformanceTestMethodRef, ...] = ()
    source_field_name: str | None = None
    limitations: tuple[str, ...] = ()
    provenance: EquipmentDataProvenance | None = None
    extraction_confidence: float | None = None
    fact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'PerformanceFact':
        expected = _digest(self.identity_payload())
        if self.fact_sha256 != expected:
            raise ValueError('fact_sha256 does not match content')
        if self.fact_id != f'pff:{expected}':
            raise ValueError('fact_id must be pff:<sha256>')
        if self.scalar_value is not None:
            _require_finite(self.scalar_value, 'scalar_value')
        if self.range_min is not None:
            _require_finite(self.range_min, 'range_min')
        if self.range_max is not None:
            _require_finite(self.range_max, 'range_max')
        if (
            self.range_min is not None
            and self.range_max is not None
            and self.range_max < self.range_min
        ):
            raise ValueError('range_max must not be below range_min')
        if self.extraction_confidence is not None:
            _require_finite(
                self.extraction_confidence, 'extraction_confidence'
            )
            if not (0.0 <= self.extraction_confidence <= 1.0):
                raise ValueError('extraction_confidence must be in [0,1]')
        # value-kind consistency — a value kind with no payload is
        # declared unknown rather than silently zero.
        if self.value_kind == 'scalar' and self.scalar_value is None:
            raise ValueError('scalar fact requires scalar_value')
        if self.value_kind == 'range' and (
            self.range_min is None or self.range_max is None
        ):
            raise ValueError('range fact requires range_min and range_max')
        if self.value_kind == 'curve_reference' and (
            not self.curve_reference
        ):
            raise ValueError('curve_reference fact requires a reference')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('fact_id', None)
        payload.pop('fact_sha256', None)
        return payload

    @property
    def is_independent_evidence(self) -> bool:
        return self.evidence_class in _INDEPENDENT_CLASSES


class PerformanceFactsImport(BaseModel):
    """One sealed ingestion run against a profile (#586 §12)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    import_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    extraction_state: PerformanceExtractionState
    source_asset_sha256: str | None = None
    source_label: str = ''
    facts: tuple[PerformanceFact, ...] = ()
    unmapped_fields: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    imported_at_utc: str = Field(min_length=1)
    import_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'PerformanceFactsImport':
        expected = _digest(self.identity_payload())
        if self.import_sha256 != expected:
            raise ValueError('import_sha256 does not match content')
        if self.import_id != f'pfi:{expected}':
            raise ValueError('import_id must be pfi:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('import_id', None)
        payload.pop('import_sha256', None)
        return payload


class PerformanceFactsEvaluation(BaseModel):
    """Sealed product-selection verdict (#586 §8)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_sha256: str = Field(pattern=_SHA256_PATTERN)
    requirement: PerformanceRequirement
    verdict: PerformanceSuitabilityVerdict
    reason_codes: tuple[str, ...] = ()
    fact_ids_used: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'PerformanceFactsEvaluation':
        expected = _digest(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('evaluation_sha256 does not match content')
        if self.evaluation_id != f'pfe:{expected}':
            raise ValueError('evaluation_id must be pfe:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('evaluation_id', None)
        payload.pop('evaluation_sha256', None)
        return payload


class PerformanceFactsProfileRebind(BaseModel):
    """Sealed review→final transition record (#586 §11)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    rebind_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    from_profile_id: str = Field(min_length=1)
    from_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    to_profile_id: str = Field(min_length=1)
    to_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_dispositions: tuple[PerformanceFieldDispositionRecord, ...] = ()
    rebound_import_ids: tuple[str, ...] = ()
    decided_at_utc: str = Field(min_length=1)
    rebind_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'PerformanceFactsProfileRebind':
        expected = _digest(self.identity_payload())
        if self.rebind_sha256 != expected:
            raise ValueError('rebind_sha256 does not match content')
        if self.rebind_id != f'pfr:{expected}':
            raise ValueError('rebind_id must be pfr:<sha256>')
        if self.from_profile_id == self.to_profile_id:
            raise ValueError('a rebind must move between two profiles')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('rebind_id', None)
        payload.pop('rebind_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def register_performance_facts_profile(
    *,
    document_id: str,
    publisher: str,
    family: PerformanceFactsFamily,
    document_reference: str,
    revision_or_date: str,
    maturity_state: PerformanceFactsMaturity,
    source_reference: str,
    field_mapping_version: str,
    field_definitions: Sequence[PerformanceFactsFieldSpec] = (),
    source_sha256: str | None = None,
    license_note: str = '',
) -> PerformanceFactsProfile:
    probe = PerformanceFactsProfile.model_construct(
        **_canon(
            PerformanceFactsProfile,
            dict(
                profile_id='',
                document_id=document_id,
                publisher=publisher,
                family=family,
                document_reference=document_reference,
                revision_or_date=revision_or_date,
                maturity_state=maturity_state,
                source_reference=source_reference,
                source_sha256=source_sha256,
                field_mapping_version=field_mapping_version,
                field_definitions=tuple(field_definitions),
                license_note=license_note,
                profile_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PerformanceFactsProfile(
        **probe.model_dump(exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'pfp:{sha}',
        profile_sha256=sha,
    )


def build_product_identity(
    *,
    document_id: str,
    manufacturer: str,
    model: str,
    **fields: Any,
) -> ManufacturerProductIdentity:
    probe = ManufacturerProductIdentity.model_construct(
        **_canon(
            ManufacturerProductIdentity,
            dict(
                product_id='',
                document_id=document_id,
                manufacturer=manufacturer,
                model=model,
                product_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return ManufacturerProductIdentity(
        **probe.model_dump(exclude={'product_id', 'product_sha256'}),
        product_id=f'mfp:{sha}',
        product_sha256=sha,
    )


def build_performance_fact(
    *,
    document_id: str,
    product: ManufacturerProductIdentity,
    quantity_kind: PerformanceQuantityKind,
    value_kind: PerformanceFactValueKind,
    **fields: Any,
) -> PerformanceFact:
    probe = PerformanceFact.model_construct(
        **_canon(
            PerformanceFact,
            dict(
                fact_id='',
                document_id=document_id,
                product_id=product.product_id,
                product_sha256=product.product_sha256,
                quantity_kind=quantity_kind,
                value_kind=value_kind,
                fact_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PerformanceFact(
        **probe.model_dump(exclude={'fact_id', 'fact_sha256'}),
        fact_id=f'pff:{sha}',
        fact_sha256=sha,
    )


def build_performance_facts_import(
    *,
    document_id: str,
    profile: PerformanceFactsProfile,
    extraction_state: PerformanceExtractionState,
    facts: Sequence[PerformanceFact],
    source_asset_sha256: str | None = None,
    source_label: str = '',
    unmapped_fields: Sequence[str] = (),
    unsupported_fields: Sequence[str] = (),
    warnings: Sequence[str] = (),
    imported_at_utc: str,
) -> PerformanceFactsImport:
    """Seal one ingestion run.

    Facts that name a ``source_field_name`` the profile does not declare
    are not rejected — they are counted in ``warnings`` and the field is
    kept verbatim on the fact (§12: preserve unknown future fields).
    """
    extra_warnings = list(warnings)
    declared = {spec.field_name for spec in profile.field_definitions}
    for fact in facts:
        if fact.source_field_name and (
            fact.source_field_name not in declared
        ):
            extra_warnings.append(
                f'fact {fact.fact_id[:20]}…: source field '
                f'{fact.source_field_name!r} is not declared by profile '
                f'{profile.document_reference!r}; value preserved verbatim'
            )
    probe = PerformanceFactsImport.model_construct(
        **_canon(
            PerformanceFactsImport,
            dict(
                import_id='',
                document_id=document_id,
                profile_id=profile.profile_id,
                profile_sha256=profile.profile_sha256,
                extraction_state=extraction_state,
                source_asset_sha256=source_asset_sha256,
                source_label=source_label,
                facts=tuple(facts),
                unmapped_fields=tuple(dict.fromkeys(unmapped_fields)),
                unsupported_fields=tuple(dict.fromkeys(unsupported_fields)),
                warnings=tuple(dict.fromkeys(extra_warnings)),
                imported_at_utc=imported_at_utc,
                import_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PerformanceFactsImport(
        **probe.model_dump(exclude={'import_id', 'import_sha256'}),
        import_id=f'pfi:{sha}',
        import_sha256=sha,
    )


def build_profile_rebind(
    *,
    document_id: str,
    from_profile: PerformanceFactsProfile,
    to_profile: PerformanceFactsProfile,
    field_dispositions: Sequence[PerformanceFieldDispositionRecord],
    rebound_import_ids: Sequence[str],
    decided_at_utc: str,
) -> PerformanceFactsProfileRebind:
    """Seal a profile-revision transition (#586 §11).

    The caller must supply explicit per-field dispositions; a field that
    changed meaning (``redefined``) requires a re-import — historical
    imports stay bound to the old profile either way.
    """
    from_fields = {s.field_name for s in from_profile.field_definitions}
    to_fields = {s.field_name for s in to_profile.field_definitions}
    declared = {d.field_name for d in field_dispositions}
    missing = (from_fields | to_fields) - declared
    if missing:
        raise ValueError(
            'rebind must dispose every profile field; missing: '
            + ', '.join(sorted(missing))
        )
    probe = PerformanceFactsProfileRebind.model_construct(
        **_canon(
            PerformanceFactsProfileRebind,
            dict(
                rebind_id='',
                document_id=document_id,
                from_profile_id=from_profile.profile_id,
                from_profile_sha256=from_profile.profile_sha256,
                to_profile_id=to_profile.profile_id,
                to_profile_sha256=to_profile.profile_sha256,
                field_dispositions=tuple(field_dispositions),
                rebound_import_ids=tuple(rebound_import_ids),
                decided_at_utc=decided_at_utc,
                rebind_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PerformanceFactsProfileRebind(
        **probe.model_dump(exclude={'rebind_id', 'rebind_sha256'}),
        rebind_id=f'pfr:{sha}',
        rebind_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Condition sufficiency + product-selection verdicts (issue #586 §4, §8)
# ---------------------------------------------------------------------------


def evaluate_fact_sufficiency(
    fact: PerformanceFact,
    spec: PerformanceFactsFieldSpec | None = None,
) -> tuple[PerformanceSufficiency, tuple[str, ...]]:
    """Rate one fact's condition completeness for engineering use.

    With no profile spec the baseline requirement applies: for
    amplifier/loudspeaker output-type quantities the conditions that make
    a bare number mean anything (#586 §4 examples) — load, channels
    driven, distortion/duration context for amplifier output; axis,
    distance, environment and tolerance for frequency/output claims.
    Returns (sufficiency, missing_condition_names).
    """
    c = fact.conditions
    required: list[str] = []
    if spec is not None:
        required.extend(spec.required_conditions)
    elif fact.quantity_kind == 'amplifier_output':
        required.extend(
            ['load_impedance_ohm', 'channels_driven', 'duration_kind']
        )
    elif fact.quantity_kind in (
        'frequency_response',
        'sensitivity',
        'max_output',
    ):
        required.extend(
            [
                'measurement_distance_m',
                'axis_azimuth_deg',
                'environment',
            ]
        )
        if fact.quantity_kind == 'frequency_response':
            required.append('frequency_domain')
    missing = c.missing(*required)
    if fact.value_kind == 'unknown':
        return 'insufficient', ('value',) + missing
    if missing:
        return (
            'insufficient' if len(missing) >= 2 else 'limited'
        ), tuple(missing)
    return 'engineering_grade', ()


def evaluate_product_suitability(
    *,
    document_id: str,
    product: ManufacturerProductIdentity,
    facts: Sequence[PerformanceFact],
    requirement: PerformanceRequirement,
    evaluated_at_utc: str,
    profile: PerformanceFactsProfile | None = None,
) -> PerformanceFactsEvaluation:
    """Sealed product-selection verdict (#586 §8).

    Missing data produces ``insufficient_data``, never a pass. Conflicts
    between manufacturer-declared and independent evidence are surfaced
    as ``conflicting_sources`` — never silently merged.
    """
    reasons: list[str] = []
    used: list[str] = []

    relevant = [
        f for f in facts if f.product_id == product.product_id
    ]
    if not relevant:
        reasons.append('no_facts_for_product')

    def _best(quantity: PerformanceQuantityKind) -> list[PerformanceFact]:
        return [f for f in relevant if f.quantity_kind == quantity]

    verdict: PerformanceSuitabilityVerdict = 'eligible'

    def _demote(v: PerformanceSuitabilityVerdict) -> None:
        nonlocal verdict
        order = [
            'eligible',
            'eligible_with_limitations',
            'insufficient_data',
            'incompatible',
        ]
        if order.index(v) > order.index(verdict):
            verdict = v

    if requirement.kind == 'output_at_distance':
        needed_db = float(requirement.params['required_db_spl'])
        distance = float(requirement.params.get('distance_m', 1.0))
        candidates = _best('max_output') + _best('sensitivity')
        usable = []
        for f in candidates:
            suff, gaps = evaluate_fact_sufficiency(f)
            used.append(f.fact_id)
            if f.value_kind == 'scalar' and f.scalar_value is not None:
                usable.append((f, suff, gaps))
            else:
                reasons.append(f'{f.quantity_kind}:unusable_value_kind')
        if not usable:
            reasons.append('no_output_facts')
            _demote('insufficient_data')
        else:
            good = [
                (f, s) for f, s, g in usable if s == 'engineering_grade'
            ]
            limited = [
                (f, s) for f, s, g in usable if s == 'limited'
            ]
            if not usable:
                pass
            elif not good and not limited:
                reasons.append('output_facts_insufficient_conditions')
                _demote('insufficient_data')
            else:
                best_val = max(
                    f.scalar_value for f, _ in good + limited
                )
                if best_val >= needed_db:
                    if not good:
                        reasons.append('output_facts_limited_conditions')
                        _demote('eligible_with_limitations')
                else:
                    reasons.append(
                        f'output_below_requirement:{best_val}<{needed_db}dB'
                    )
                    _demote('incompatible')
                indep = any(f.is_independent_evidence for f, _ in usable)
                manuf = any(
                    not f.is_independent_evidence for f, _ in usable
                )
                if indep and manuf:
                    vals = {
                        'independent': max(
                            f.scalar_value
                            for f, _ in usable
                            if f.is_independent_evidence
                        ),
                        'manufacturer': max(
                            f.scalar_value
                            for f, _ in usable
                            if not f.is_independent_evidence
                        ),
                    }
                    if abs(vals['independent'] - vals['manufacturer']) > 1e-9:
                        reasons.append('conflicting_sources')
                        if verdict == 'eligible':
                            _demote('eligible_with_limitations')
        _ = distance

    elif requirement.kind == 'bandwidth':
        low = float(requirement.params['required_low_hz'])
        high = float(requirement.params['required_high_hz'])
        fr = _best('frequency_response')
        if not fr:
            reasons.append('no_frequency_response_facts')
            _demote('insufficient_data')
        else:
            covering = [
                f for f in fr
                if f.domain is not None
                and f.domain.minimum_hz <= low
                and f.domain.maximum_hz >= high
            ]
            if not covering:
                reasons.append('bandwidth_domain_gap')
                _demote('insufficient_data')
            else:
                for f in covering:
                    used.append(f.fact_id)
                suffs = [
                    evaluate_fact_sufficiency(f) for f in covering
                ]
                if not any(s == 'engineering_grade' for s, _ in suffs):
                    reasons.append('bandwidth_facts_limited_conditions')
                    _demote('eligible_with_limitations')

    elif requirement.kind == 'amplifier_capability':
        load = float(requirement.params['load_ohm'])
        watts = float(requirement.params['required_w'])
        amp = _best('amplifier_output')
        if not amp:
            reasons.append('no_amplifier_facts')
            _demote('insufficient_data')
        else:
            for f in amp:
                used.append(f.fact_id)
            adequate = [
                f for f in amp
                if f.scalar_value is not None
                and f.scalar_value >= watts
                and f.conditions.load_impedance_ohm == load
                and f.conditions.duration_kind == 'continuous'
            ]
            matching = [
                f for f in amp
                if f.conditions.load_impedance_ohm == load
            ]
            if adequate:
                pass
            elif matching:
                reasons.append('amplifier_insufficient_continuous_power')
                _demote('incompatible')
            else:
                reasons.append('amplifier_load_condition_missing')
                _demote('insufficient_data')

    elif requirement.kind in (
        'directivity_compatible',
        'physical_envelope',
        'generic',
    ):
        quantity = {
            'directivity_compatible': 'directivity',
            'physical_envelope': 'screen_optical',
            'generic': 'other_declared',
        }[requirement.kind]
        matching = _best(quantity)  # type: ignore[arg-type]
        if not matching:
            reasons.append(f'no_{quantity}_facts')
            _demote('insufficient_data')
        else:
            for f in matching:
                used.append(f.fact_id)
            _demote('eligible_with_limitations')

    else:
        reasons.append('unknown_requirement_kind')
        _demote('insufficient_data')

    if not reasons:
        reasons.append('all_requirements_satisfied')

    probe = PerformanceFactsEvaluation.model_construct(
        **_canon(
            PerformanceFactsEvaluation,
            dict(
                evaluation_id='',
                document_id=document_id,
                product_id=product.product_id,
                product_sha256=product.product_sha256,
                requirement=requirement,
                verdict=verdict,
                reason_codes=tuple(dict.fromkeys(reasons)),
                fact_ids_used=tuple(dict.fromkeys(used)),
                evaluated_at_utc=evaluated_at_utc,
                evaluation_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PerformanceFactsEvaluation(
        **probe.model_dump(exclude={'evaluation_id', 'evaluation_sha256'}),
        evaluation_id=f'pfe:{sha}',
        evaluation_sha256=sha,
    )


def reconcile_conflicting_facts(
    *,
    product_id: str,
    quantity_kind: PerformanceQuantityKind,
    facts: Sequence[PerformanceFact],
) -> dict[str, Any]:
    """Coexistence view over conflicting sources (issue #586 §9).

    Every matching fact is returned with its evidence class — no source
    overwrites another, no merging. The view marks whether independent
    and manufacturer sources disagree on the scalar claim.
    """
    matching = [
        f
        for f in facts
        if f.product_id == product_id and f.quantity_kind == quantity_kind
    ]
    by_class: dict[str, list[str]] = {}
    for f in matching:
        by_class.setdefault(f.evidence_class, []).append(f.fact_id)
    scalars = {
        f.evidence_class: f.scalar_value
        for f in matching
        if f.scalar_value is not None
    }
    disagrees = (
        len({round(v, 9) for v in scalars.values()}) > 1
        if len(scalars) > 1
        else False
    )
    return {
        'product_id': product_id,
        'quantity_kind': quantity_kind,
        'fact_count': len(matching),
        'by_evidence_class': by_class,
        'scalar_disagreement': disagrees,
        'state': 'coexisting' if matching else 'no_facts',
    }


def rp22_consumption_view(
    facts: Sequence[PerformanceFact],
    parameter_id: str,
) -> 'RP22ParameterObservation':
    """Map one compatible fact into an RP22 observation (#586 §7).

    The returned observation is *always* ``design_prediction`` evidence —
    manufacturer facts never become commissioned room performance. The
    dynamics basis is capped at ``nominal_spec_only`` /
    ``modelled_with_output_limits``; only HTDT-measured evidence could go
    further and this path never produces it.
    """
    from .cad_rp22_profile import RP22ParameterObservation

    compatible = [
        f for f in facts if f.value_kind == 'scalar' and f.scalar_value
        is not None
    ]
    if not compatible:
        return RP22ParameterObservation(
            parameter_id=parameter_id,
            value=None,
            evidence_class='unknown',
            note='no compatible RP1 facts — missing stays missing',
        )
    fact = compatible[0]
    basis = 'nominal_spec_only'
    suff, _gaps = evaluate_fact_sufficiency(fact)
    if suff == 'engineering_grade' and fact.quantity_kind in (
        'amplifier_output',
        'max_output',
    ):
        basis = 'modelled_with_output_limits'
    return RP22ParameterObservation(
        parameter_id=parameter_id,
        value=fact.scalar_value,
        evidence_class='design_prediction',
        evidence_ref=fact.fact_id,
        basis=basis,
        note=(
            f'manufacturer-declared {fact.quantity_kind} fact; '
            'not commissioning evidence'
        ),
    )


__all__ = [
    'PERFORMANCE_FACTS_AUTHORITY_VERSION',
    'PERFORMANCE_FACTS_SCHEMA_VERSION',
    'ManufacturerProductIdentity',
    'PerformanceEnvironment',
    'PerformanceDurationKind',
    'PerformanceExtractionState',
    'PerformanceFact',
    'PerformanceFactConditions',
    'PerformanceFactValueKind',
    'PerformanceFactsEvaluation',
    'PerformanceFactsFamily',
    'PerformanceFactsFieldSpec',
    'PerformanceFactsImport',
    'PerformanceFactsMaturity',
    'PerformanceFactsProfile',
    'PerformanceFactsProfileRebind',
    'PerformanceFieldDisposition',
    'PerformanceFieldDispositionRecord',
    'PerformanceQuantityKind',
    'PerformanceRequirement',
    'PerformanceRequirementKind',
    'PerformanceSufficiency',
    'PerformanceSuitabilityVerdict',
    'PerformanceTestMethodRef',
    'build_performance_fact',
    'build_performance_facts_import',
    'build_product_identity',
    'build_profile_rebind',
    'evaluate_fact_sufficiency',
    'evaluate_product_suitability',
    'reconcile_conflicting_facts',
    'register_performance_facts_profile',
    'rp22_consumption_view',
]
