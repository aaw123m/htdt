"""Typed replayable evidence authorities behind O100C EquipmentDefinition data.

An ``EquipmentDataProvenance`` claim on a persisted EquipmentDefinition is only
a source *claim*: an evidence kind plus a source name/version/reference and a
SHA-256. It does not by itself prove that the claimed source exists or that the
normalized values carried by the definition were ever derived from it.

This module defines the immutable, content-addressed
``EquipmentEvidenceAuthority`` record that resolves such a claim:

- ``managed_source_asset`` — the exact source document/data bytes are retained
  as a content-addressed managed asset (the shared ``measurement-assets``
  contract used by measurement, treatment and directivity evidence, so the
  native backup/restore path preserves them for replay). The record carries
  the extraction identity/version and the datum locator (page/table/field);
  when the registered normalized-JSON extractor is named, the recorded subject
  must replay exactly from the retained bytes.
- ``external_source`` — a typed immutable external authority for source
  documents that cannot be retained locally: an exact URI and locator bound to
  the claimed source identity.
- ``manual_entry`` — an explicit immutable manual evidence record carrying the
  actor, timestamp, citation and optional confidence behind user-entered data.

Every authority is bound to one exact ``equipment_definition_sha256`` and one
exact provenance claim, and its ``subject`` records the normalized field-group
values it supports — sensitivity/reference, SPL capability, uncertainty,
directivity capability metadata, and the identity/geometry/installation groups
covered by definition-level provenance. On save and on every authoritative
read the repository re-resolves each cited provenance, re-verifies retained
bytes, re-derives replayable extractions and requires the recorded subject to
reproduce the definition's normalized values exactly; any divergence fails
closed. A naked SHA-256 alone therefore cannot authorize production
sensitivity/SPL/uncertainty data.

Directivity *raw dataset* assets remain the #357 authority
(``CadDirectivityRepository``); the equipment evidence here asserts the
capability metadata and may share the same managed asset row rather than
duplicating bytes. Wave-excitation source evidence stays coordinated with the
R110 excitation authority (#409).
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import (
    DirectivityCapability,
    DirectivityCapabilityTier,
    DirectivityDataFormat,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentDefinition,
    EquipmentIdentityKind,
    EquipmentUncertainty,
    FrequencyDomain,
    InterpolationMethod,
    InterpolationProvenance,
    MountingMetadata,
    PortMetadata,
    ClearanceMetadata,
    SensitivityReference,
    SplCapability,
)
from .cad_scene import Offset3, Size3


EQUIPMENT_EVIDENCE_AUTHORITY_VERSION = 'equipment-evidence-1'

# Normalized JSON source-document contract: a retained source asset declared
# with this schema and the registered extractor id/version re-derives its
# recorded subject from the exact bytes on every authoritative read.
EQUIPMENT_EVIDENCE_JSON_SCHEMA = 'htdt.equipment-evidence.v1'
EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID = 'htdt.equipment-evidence-json'
EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION = '1'

EquipmentEvidenceFieldGroup = Literal[
    'identity',
    'cabinet_geometry',
    'installation',
    'sensitivity_reference',
    'spl_capability',
    'uncertainty',
    'directivity_capability',
    'directivity_interpolation',
]

# Field groups that have no provenance-bearing field of their own and are
# covered by the definition-level provenance tuple.
EQUIPMENT_STRUCTURAL_FIELD_GROUPS: tuple[EquipmentEvidenceFieldGroup, ...] = (
    'identity',
    'cabinet_geometry',
    'installation',
)

EquipmentEvidenceAuthorityKind = Literal[
    'managed_source_asset',
    'external_source',
    'manual_entry',
]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def provenance_sha256(provenance: EquipmentDataProvenance) -> str:
    """Canonical content address of one provenance claim."""

    return _digest(provenance.model_dump(mode='json'))


class EquipmentIdentitySubject(BaseModel):
    """Equipment identity values an evidence authority supports."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    identity_kind: EquipmentIdentityKind
    manufacturer: str | None = None
    model: str | None = None
    user_label: str | None = None


class EquipmentCabinetGeometrySubject(BaseModel):
    """Cabinet envelope and acoustic reference point values."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    cabinet_envelope_m: Size3
    acoustic_reference_point_m: Offset3


class EquipmentInstallationSubject(BaseModel):
    """Mounting, port and clearance metadata values."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    mounting: MountingMetadata
    port: PortMetadata
    clearance: ClearanceMetadata


class EquipmentSensitivitySubject(BaseModel):
    """SensitivityReference values minus the provenance claim itself."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    level_db_spl: float
    input_quantity: Literal['voltage_v_rms', 'power_w']
    input_value: float = Field(gt=0.0)
    distance_m: float = Field(gt=0.0)
    valid_frequency_domain: FrequencyDomain | None = None
    weighting: str | None = Field(default=None, min_length=1)

    @field_validator('level_db_spl', 'input_value', 'distance_m')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='sensitivity evidence value')

    @classmethod
    def from_reference(
        cls,
        reference: SensitivityReference,
    ) -> 'EquipmentSensitivitySubject':
        return cls.model_validate(
            reference.model_dump(mode='python', exclude={'provenance'})
        )


class EquipmentSplSubject(BaseModel):
    """SplCapability values minus the provenance claim itself."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    continuous_db_spl: float | None = None
    peak_db_spl: float | None = None
    reference_distance_m: float = Field(gt=0.0)
    valid_frequency_domain: FrequencyDomain | None = None
    continuous_duration_s: float | None = Field(default=None, gt=0.0)
    peak_duration_s: float | None = Field(default=None, gt=0.0)
    declared_headroom_db: float | None = Field(default=None, ge=0.0)
    headroom_reference_level_db_spl: float | None = None

    @field_validator(
        'continuous_db_spl',
        'peak_db_spl',
        'reference_distance_m',
        'continuous_duration_s',
        'peak_duration_s',
        'declared_headroom_db',
        'headroom_reference_level_db_spl',
    )
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='SPL capability evidence value')

    @classmethod
    def from_capability(
        cls,
        capability: SplCapability,
    ) -> 'EquipmentSplSubject':
        return cls.model_validate(
            capability.model_dump(mode='python', exclude={'provenance'})
        )


class EquipmentUncertaintySubject(BaseModel):
    """One EquipmentUncertainty item minus the provenance claim itself."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    model: Literal['bounded', 'standard_uncertainty', 'statement']
    lower: float | None = None
    upper: float | None = None
    standard_uncertainty: float | None = Field(default=None, ge=0.0)
    statement: str | None = Field(default=None, min_length=1)

    @field_validator('lower', 'upper', 'standard_uncertainty')
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='uncertainty evidence value')

    @classmethod
    def from_item(
        cls,
        item: EquipmentUncertainty,
    ) -> 'EquipmentUncertaintySubject':
        return cls.model_validate(
            item.model_dump(mode='python', exclude={'provenance'})
        )


class EquipmentInterpolationSubject(BaseModel):
    """InterpolationProvenance values minus the provenance claim itself."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    method: InterpolationMethod
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)

    @classmethod
    def from_interpolation(
        cls,
        interpolation: InterpolationProvenance,
    ) -> 'EquipmentInterpolationSubject':
        return cls.model_validate(
            interpolation.model_dump(mode='python', exclude={'provenance'})
        )


class EquipmentDirectivitySubject(BaseModel):
    """DirectivityCapability metadata minus provenance and interpolation.

    The interpolation authority is its own field group
    (``directivity_interpolation``); raw imported sample assets remain bound
    through the #357 DirectivityDataset authority.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    tier: DirectivityCapabilityTier
    data_format: DirectivityDataFormat
    data_asset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    valid_domain: DirectivityDomain | None = None
    coherent_phase: bool = False
    phase_reference: str | None = Field(default=None, min_length=1)
    analytic_model: str | None = Field(default=None, min_length=1)

    @classmethod
    def from_capability(
        cls,
        capability: DirectivityCapability,
    ) -> 'EquipmentDirectivitySubject':
        return cls.model_validate(
            capability.model_dump(
                mode='python',
                exclude={'provenance', 'interpolation'},
            )
        )


class EquipmentEvidenceSubject(BaseModel):
    """Normalized field-group values an evidence authority supports.

    Every populated group records the exact normalized value the bound
    EquipmentDefinition must carry (with the citing provenance stripped, so the
    record never asserts its own claim). ``uncertainty`` lists the individual
    uncertainty items the authority supports; other groups are present-or-absent
    single values.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    identity: EquipmentIdentitySubject | None = None
    cabinet_geometry: EquipmentCabinetGeometrySubject | None = None
    installation: EquipmentInstallationSubject | None = None
    sensitivity_reference: EquipmentSensitivitySubject | None = None
    spl_capability: EquipmentSplSubject | None = None
    uncertainty: tuple[EquipmentUncertaintySubject, ...] = ()
    directivity_capability: EquipmentDirectivitySubject | None = None
    directivity_interpolation: EquipmentInterpolationSubject | None = None

    @model_validator(mode='after')
    def non_empty_subject(self) -> 'EquipmentEvidenceSubject':
        if not self.supported_groups():
            raise ValueError(
                'equipment evidence subject requires at least one field group'
            )
        quantities = [item.quantity for item in self.uncertainty]
        if len(quantities) != len(set(quantities)):
            raise ValueError(
                'equipment evidence subject uncertainty quantities must be unique'
            )
        return self

    def supported_groups(self) -> frozenset[EquipmentEvidenceFieldGroup]:
        groups: set[EquipmentEvidenceFieldGroup] = set()
        if self.identity is not None:
            groups.add('identity')
        if self.cabinet_geometry is not None:
            groups.add('cabinet_geometry')
        if self.installation is not None:
            groups.add('installation')
        if self.sensitivity_reference is not None:
            groups.add('sensitivity_reference')
        if self.spl_capability is not None:
            groups.add('spl_capability')
        if self.uncertainty:
            groups.add('uncertainty')
        if self.directivity_capability is not None:
            groups.add('directivity_capability')
        if self.directivity_interpolation is not None:
            groups.add('directivity_interpolation')
        return frozenset(groups)

    def group_value(self, group: EquipmentEvidenceFieldGroup) -> Any:
        return getattr(self, group)


class ManagedEquipmentSourceAsset(BaseModel):
    """Retained exact source document/data asset plus its extraction authority.

    ``source_asset_sha256`` is the content address of the retained bytes in the
    shared managed asset store and must equal the citing provenance's
    ``source_sha256``. ``extractor_id``/``extractor_version`` name the
    parser/extraction/conversion authority; the registered normalized-JSON
    extractor replays the recorded subject from the exact bytes.
    ``datum_locator`` preserves page/table/field provenance inside the source.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)
    extractor_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    filename: str | None = Field(default=None, min_length=1)
    media_type: str | None = Field(default=None, min_length=1)
    declared_schema: str | None = Field(default=None, min_length=1)
    datum_locator: str | None = Field(default=None, min_length=1)


class ExternalEquipmentAuthority(BaseModel):
    """Typed immutable external authority for sources not retained locally."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_uri: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    retrieved_at_utc: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)


class ManualEquipmentEvidence(BaseModel):
    """Explicit immutable manual evidence record behind user-entered data."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    actor: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    confidence: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)


class EquipmentEvidenceAuthority(BaseModel):
    """Immutable content-addressed evidence record behind a provenance claim.

    The record binds one exact ``equipment_definition_sha256`` and one exact
    ``EquipmentDataProvenance`` to the normalized ``subject`` values it
    supports. ``evidence_sha256``/``evidence_id`` are derived from the full
    payload, so a retained record can never be retargeted to another
    definition, source or value set.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'equipment-evidence-1'
    ] = EQUIPMENT_EVIDENCE_AUTHORITY_VERSION
    evidence_id: str = Field(pattern=r'^equipment-evidence:[0-9a-f]{64}$')

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    provenance: EquipmentDataProvenance
    authority_kind: EquipmentEvidenceAuthorityKind
    managed_asset: ManagedEquipmentSourceAsset | None = None
    external: ExternalEquipmentAuthority | None = None
    manual: ManualEquipmentEvidence | None = None
    subject: EquipmentEvidenceSubject

    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'EquipmentEvidenceAuthority':
        carriers = {
            'managed_source_asset': self.managed_asset,
            'external_source': self.external,
            'manual_entry': self.manual,
        }
        if carriers[self.authority_kind] is None:
            raise ValueError(
                f"{self.authority_kind} evidence requires its typed "
                'authority record'
            )
        extras = [
            kind
            for kind, record in carriers.items()
            if kind != self.authority_kind and record is not None
        ]
        if extras:
            raise ValueError(
                'equipment evidence cannot carry authority records for other '
                f'kinds: {extras}'
            )
        if (
            self.managed_asset is not None
            and self.managed_asset.source_asset_sha256
            != self.provenance.source_sha256
        ):
            raise ValueError(
                'managed source asset hash does not match the provenance '
                'source_sha256 it resolves'
            )
        expected = _digest(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError('EquipmentEvidenceAuthority semantic hash mismatch')
        if self.evidence_id != f'equipment-evidence:{expected}':
            raise ValueError('EquipmentEvidenceAuthority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evidence_id', 'evidence_sha256'},
        )

    def subject_sha256(self) -> str:
        return _digest(self.subject.model_dump(mode='json'))

    def field_groups(self) -> tuple[EquipmentEvidenceFieldGroup, ...]:
        return tuple(
            group
            for group in (
                'identity',
                'cabinet_geometry',
                'installation',
                'sensitivity_reference',
                'spl_capability',
                'uncertainty',
                'directivity_capability',
                'directivity_interpolation',
            )
            if group in self.subject.supported_groups()
        )


def equipment_field_group_subjects(
    definition: EquipmentDefinition,
) -> dict[str, Any]:
    """Normalized subject value for every field group the definition carries.

    Values are provenance-stripped: the evidence subject asserts the normalized
    data, never the claim itself.
    """

    subjects: dict[str, Any] = {
        'identity': EquipmentIdentitySubject(
            identity_kind=definition.identity_kind,
            manufacturer=definition.manufacturer,
            model=definition.model,
            user_label=definition.user_label,
        ),
        'cabinet_geometry': EquipmentCabinetGeometrySubject(
            cabinet_envelope_m=definition.cabinet_envelope_m,
            acoustic_reference_point_m=definition.acoustic_reference_point_m,
        ),
        'installation': EquipmentInstallationSubject(
            mounting=definition.mounting,
            port=definition.port,
            clearance=definition.clearance,
        ),
        'directivity_capability': EquipmentDirectivitySubject.from_capability(
            definition.directivity
        ),
    }
    if definition.sensitivity is not None:
        subjects['sensitivity_reference'] = (
            EquipmentSensitivitySubject.from_reference(definition.sensitivity)
        )
    if definition.spl_capability is not None:
        subjects['spl_capability'] = EquipmentSplSubject.from_capability(
            definition.spl_capability
        )
    if definition.uncertainty:
        subjects['uncertainty'] = tuple(
            EquipmentUncertaintySubject.from_item(item)
            for item in definition.uncertainty
        )
    if definition.directivity.interpolation is not None:
        subjects['directivity_interpolation'] = (
            EquipmentInterpolationSubject.from_interpolation(
                definition.directivity.interpolation
            )
        )
    return subjects


def equipment_provenance_items(
    definition: EquipmentDefinition,
) -> tuple[EquipmentDataProvenance, ...]:
    """Every distinct provenance claim cited anywhere in the definition."""

    items: list[EquipmentDataProvenance] = list(definition.provenance)
    if definition.sensitivity is not None:
        items.append(definition.sensitivity.provenance)
    if definition.spl_capability is not None:
        items.append(definition.spl_capability.provenance)
    items.extend(item.provenance for item in definition.uncertainty)
    items.append(definition.directivity.provenance)
    if definition.directivity.interpolation is not None:
        items.append(definition.directivity.interpolation.provenance)

    unique: list[EquipmentDataProvenance] = []
    seen: set[str] = set()
    for item in items:
        key = provenance_sha256(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def equipment_provenance_field_groups(
    definition: EquipmentDefinition,
    provenance: EquipmentDataProvenance,
) -> frozenset[EquipmentEvidenceFieldGroup]:
    """Field groups a provenance claim is attached to inside the definition.

    Definition-level provenance items are attached to the structural trio
    (identity, cabinet geometry, installation); field-level claims attach to
    their own group.
    """

    groups: set[EquipmentEvidenceFieldGroup] = set()
    if any(item == provenance for item in definition.provenance):
        groups.update(EQUIPMENT_STRUCTURAL_FIELD_GROUPS)
    if (
        definition.sensitivity is not None
        and definition.sensitivity.provenance == provenance
    ):
        groups.add('sensitivity_reference')
    if (
        definition.spl_capability is not None
        and definition.spl_capability.provenance == provenance
    ):
        groups.add('spl_capability')
    if any(item.provenance == provenance for item in definition.uncertainty):
        groups.add('uncertainty')
    if definition.directivity.provenance == provenance:
        groups.add('directivity_capability')
    if (
        definition.directivity.interpolation is not None
        and definition.directivity.interpolation.provenance == provenance
    ):
        groups.add('directivity_interpolation')
    return frozenset(groups)


def equipment_evidence_subject(
    definition: EquipmentDefinition,
    *,
    groups: Sequence[EquipmentEvidenceFieldGroup] | None = None,
    uncertainty_quantities: Iterable[str] | None = None,
) -> EquipmentEvidenceSubject:
    """Derive an evidence subject from a definition's normalized values.

    ``groups`` selects which field groups the subject supports (default: every
    group the definition carries). ``uncertainty_quantities`` selects which
    uncertainty items the subject asserts (default: all of them).
    """

    available = equipment_field_group_subjects(definition)
    selected = (
        tuple(available)
        if groups is None
        else tuple(dict.fromkeys(groups))
    )
    unknown = sorted(set(selected) - set(available) - {'uncertainty'})
    if unknown:
        raise ValueError(
            'equipment definition carries no values for field groups: '
            f'{unknown}'
        )
    kwargs: dict[str, Any] = {}
    for group in selected:
        if group == 'uncertainty':
            items = tuple(available.get('uncertainty', ()))
            if uncertainty_quantities is not None:
                wanted = set(uncertainty_quantities)
                items = tuple(
                    item for item in items if item.quantity in wanted
                )
            if items:
                kwargs['uncertainty'] = items
            continue
        if group not in available:
            raise ValueError(
                f'equipment definition carries no {group!r} field group values'
            )
        kwargs[group] = available[group]
    return EquipmentEvidenceSubject(**kwargs)


def build_equipment_evidence_authority(
    *,
    definition: EquipmentDefinition,
    provenance: EquipmentDataProvenance,
    subject: EquipmentEvidenceSubject,
    managed_asset: ManagedEquipmentSourceAsset | None = None,
    external: ExternalEquipmentAuthority | None = None,
    manual: ManualEquipmentEvidence | None = None,
) -> EquipmentEvidenceAuthority:
    """Build an immutable evidence authority bound to an exact definition."""

    carriers = {
        'managed_source_asset': managed_asset,
        'external_source': external,
        'manual_entry': manual,
    }
    provided = [kind for kind, record in carriers.items() if record is not None]
    if len(provided) != 1:
        raise ValueError(
            'exactly one typed evidence authority record is required'
        )
    authority_kind = provided[0]
    if not equipment_provenance_field_groups(definition, provenance):
        raise ValueError(
            'equipment evidence provenance is not cited by the bound '
            'EquipmentDefinition'
        )

    core = {
        'authority_version': EQUIPMENT_EVIDENCE_AUTHORITY_VERSION,
        'equipment_definition_id': definition.definition_id,
        'equipment_definition_version': definition.version,
        'equipment_definition_sha256': definition.semantic_sha256,
        'provenance': provenance.model_dump(mode='json'),
        'authority_kind': authority_kind,
        'managed_asset': (
            None if managed_asset is None else managed_asset.model_dump(mode='json')
        ),
        'external': (
            None if external is None else external.model_dump(mode='json')
        ),
        'manual': (
            None if manual is None else manual.model_dump(mode='json')
        ),
        'subject': subject.model_dump(mode='json'),
    }
    digest = _digest(core)
    return EquipmentEvidenceAuthority(
        evidence_id=f'equipment-evidence:{digest}',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
        provenance=provenance,
        authority_kind=authority_kind,  # type: ignore[arg-type]
        managed_asset=managed_asset,
        external=external,
        manual=manual,
        subject=subject,
        evidence_sha256=digest,
    )


def build_equipment_manual_evidence(
    definition: EquipmentDefinition,
    *,
    actor: str,
    recorded_at_utc: str,
    citation: str | None = None,
    confidence: str | None = None,
    note: str | None = None,
) -> tuple[EquipmentEvidenceAuthority, ...]:
    """Manual evidence authorities covering every cited provenance claim.

    Each distinct provenance claim cited by the definition resolves to one
    immutable manual record whose subject supports exactly the field groups
    that claim is attached to. This is the manual-entry authoring path; it does
    not bypass the save/read re-resolution contract.
    """

    authorities: list[EquipmentEvidenceAuthority] = []
    for provenance in equipment_provenance_items(definition):
        groups = equipment_provenance_field_groups(definition, provenance)
        quantities = (
            item.quantity
            for item in definition.uncertainty
            if item.provenance == provenance
        )
        subject = equipment_evidence_subject(
            definition,
            groups=sorted(groups),
            uncertainty_quantities=quantities,
        )
        authorities.append(
            build_equipment_evidence_authority(
                definition=definition,
                provenance=provenance,
                subject=subject,
                manual=ManualEquipmentEvidence(
                    actor=actor,
                    recorded_at_utc=recorded_at_utc,
                    citation=citation or provenance.source_reference,
                    confidence=confidence,
                    note=note,
                ),
            )
        )
    return tuple(authorities)


class EquipmentEvidenceDocumentV1(BaseModel):
    """Normalized JSON equipment-evidence source document (v1)."""

    model_config = ConfigDict(
        frozen=True,
        extra='forbid',
        populate_by_name=True,
    )

    schema_id: Literal['htdt.equipment-evidence.v1'] = Field(
        default=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
        alias='schema',
    )
    subject: EquipmentEvidenceSubject


def replay_equipment_evidence_extraction(
    *,
    source_bytes: bytes,
    extractor_id: str,
    extractor_version: str,
) -> EquipmentEvidenceSubject:
    """Re-derive an evidence subject from retained normalized source bytes.

    Only the registered extractor may replay; any other id/version fails
    closed so an unrecorded transformation can never stand in for evidence.
    """

    if (extractor_id, extractor_version) != (
        EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
        EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
    ):
        raise ValueError(
            'no supported equipment evidence extractor for '
            f'{extractor_id!r} {extractor_version!r}'
        )
    document = EquipmentEvidenceDocumentV1.model_validate_json(source_bytes)
    return document.subject


class EquipmentFieldEvidenceRef(BaseModel):
    """Typed reference from one field group to its resolved evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    field_group: EquipmentEvidenceFieldGroup
    evidence_id: str = Field(pattern=r'^equipment-evidence:[0-9a-f]{64}$')
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ResolvedEquipmentEvidence(BaseModel):
    """Authoritative resolution of every evidence claim on a definition."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    field_evidence: tuple[EquipmentFieldEvidenceRef, ...]
    authorities: tuple[EquipmentEvidenceAuthority, ...]
