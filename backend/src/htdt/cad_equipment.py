from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_scene import Offset3, Size3


EQUIPMENT_DEFINITION_SCHEMA_VERSION = 1
EQUIPMENT_DEFINITION_AUTHORITY_VERSION = 'o100c-equipment-definition-1'

EquipmentIdentityKind = Literal['manufacturer', 'user_defined']
EquipmentEvidenceKind = Literal[
    'measured',
    'manufacturer',
    'inferred',
    'analytic',
    'user_defined',
]
DirectivityCapabilityTier = Literal[
    'complex',
    'magnitude_only',
    'polar_summary',
    'analytic',
    'unknown',
]
DirectivityDataFormat = Literal[
    'sofa_aes69',
    'clf',
    'cf2',
    'polar_table',
    'cta2034_summary',
    'analytic_model',
    'custom',
    'unknown',
]
InterpolationMethod = Literal[
    'none',
    'nearest',
    'linear',
    'log_frequency_linear_angle',
    'spherical',
    'custom',
]
EquipmentCapabilityClaim = Literal[
    'cabinet_geometry',
    'sensitivity_reference',
    'continuous_spl',
    'peak_spl',
    'directivity_summary',
    'directivity_magnitude',
    'directivity_complex',
    'coherent_phase',
]
EquipmentCapabilityDecision = Literal['SUPPORTED', 'UNSUPPORTED']


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


class EquipmentDataProvenance(BaseModel):
    """Exact evidence/source record for one equipment datum or data bundle."""

    model_config = ConfigDict(frozen=True)

    evidence_kind: EquipmentEvidenceKind
    source_name: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class FrequencyDomain(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum_hz: float = Field(gt=0.0)
    maximum_hz: float = Field(gt=0.0)

    @field_validator('minimum_hz', 'maximum_hz')
    @classmethod
    def finite_frequency(cls, value: float) -> float:
        return _finite(value, field_name='frequency')

    @model_validator(mode='after')
    def valid_range(self) -> 'FrequencyDomain':
        if self.maximum_hz <= self.minimum_hz:
            raise ValueError('frequency domain maximum must exceed minimum')
        return self

    def contains(self, frequency_hz: float) -> bool:
        value = _finite(frequency_hz, field_name='frequency_hz')
        return self.minimum_hz <= value <= self.maximum_hz


class AngleDomain(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum_deg: float
    maximum_deg: float

    @field_validator('minimum_deg', 'maximum_deg')
    @classmethod
    def finite_angle(cls, value: float) -> float:
        return _finite(value, field_name='angle')

    @model_validator(mode='after')
    def valid_range(self) -> 'AngleDomain':
        if self.maximum_deg < self.minimum_deg:
            raise ValueError('angle domain maximum must not be below minimum')
        return self

    def contains(self, angle_deg: float) -> bool:
        value = _finite(angle_deg, field_name='angle_deg')
        return self.minimum_deg <= value <= self.maximum_deg


class DirectivityDomain(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency: FrequencyDomain
    horizontal: AngleDomain
    vertical: AngleDomain


class InterpolationProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: InterpolationMethod
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    provenance: EquipmentDataProvenance


class DirectivityCapability(BaseModel):
    """Source directivity capability without silently upgrading missing phase."""

    model_config = ConfigDict(frozen=True)

    tier: DirectivityCapabilityTier
    data_format: DirectivityDataFormat
    provenance: EquipmentDataProvenance
    data_asset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    valid_domain: DirectivityDomain | None = None
    interpolation: InterpolationProvenance | None = None
    coherent_phase: bool = False
    phase_reference: str | None = Field(default=None, min_length=1)
    analytic_model: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_capability(self) -> 'DirectivityCapability':
        if self.tier == 'unknown':
            if self.data_format != 'unknown':
                raise ValueError('unknown directivity must use unknown data format')
            if any((
                self.data_asset_sha256 is not None,
                self.valid_domain is not None,
                self.interpolation is not None,
                self.coherent_phase,
                self.phase_reference is not None,
                self.analytic_model is not None,
            )):
                raise ValueError('unknown directivity must not carry fabricated capability data')
            return self

        if self.valid_domain is None:
            raise ValueError('known directivity requires an explicit valid domain')
        if self.interpolation is None:
            raise ValueError(
                'known directivity requires explicit interpolation method/provenance'
            )

        if self.tier == 'analytic':
            if self.data_format != 'analytic_model' or self.analytic_model is None:
                raise ValueError('analytic directivity requires an explicit analytic model')
            if self.data_asset_sha256 is not None:
                raise ValueError('analytic directivity must not masquerade as imported data')
        else:
            if self.data_format in {'analytic_model', 'unknown'}:
                raise ValueError('imported directivity requires an imported-data format')
            if self.data_asset_sha256 is None:
                raise ValueError('imported directivity requires exact source asset hash')
            if self.analytic_model is not None:
                raise ValueError('imported directivity must not carry an analytic model')

        if self.tier == 'complex':
            if not self.coherent_phase or self.phase_reference is None:
                raise ValueError('complex directivity requires explicit coherent phase reference')
        elif self.tier in {'magnitude_only', 'polar_summary'}:
            if self.coherent_phase or self.phase_reference is not None:
                raise ValueError(
                    'magnitude-only/polar directivity cannot claim coherent phase'
                )
        elif self.tier == 'analytic':
            if self.coherent_phase != (self.phase_reference is not None):
                raise ValueError(
                    'analytic coherent phase requires an explicit phase reference'
                )
        return self


class MountingMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    mounting_modes: tuple[
        Literal[
            'free_standing',
            'stand',
            'shelf',
            'wall',
            'ceiling',
            'in_wall',
            'in_ceiling',
            'custom',
        ],
        ...,
    ] = ()
    orientation_notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def unique_modes(self) -> 'MountingMetadata':
        if len(self.mounting_modes) != len(set(self.mounting_modes)):
            raise ValueError('mounting modes must be unique')
        return self


class PortMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    port_type: Literal[
        'sealed',
        'front',
        'rear',
        'side',
        'down',
        'passive_radiator',
        'other',
        'unknown',
    ] = 'unknown'
    minimum_clearance_m: float | None = Field(default=None, ge=0.0)

    @field_validator('minimum_clearance_m')
    @classmethod
    def finite_clearance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='minimum_clearance_m')


class ClearanceMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    front_m: float | None = Field(default=None, ge=0.0)
    rear_m: float | None = Field(default=None, ge=0.0)
    side_m: float | None = Field(default=None, ge=0.0)
    top_m: float | None = Field(default=None, ge=0.0)
    bottom_m: float | None = Field(default=None, ge=0.0)

    @field_validator('front_m', 'rear_m', 'side_m', 'top_m', 'bottom_m')
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='clearance')


class SensitivityReference(BaseModel):
    """Sensitivity/reference level with explicit excitation and distance semantics."""

    model_config = ConfigDict(frozen=True)

    level_db_spl: float
    input_quantity: Literal['voltage_v_rms', 'power_w']
    input_value: float = Field(gt=0.0)
    distance_m: float = Field(gt=0.0)
    valid_frequency_domain: FrequencyDomain | None = None
    weighting: str | None = Field(default=None, min_length=1)
    provenance: EquipmentDataProvenance

    @field_validator('level_db_spl', 'input_value', 'distance_m')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='sensitivity value')


class SplCapability(BaseModel):
    """Declared level capability; no target-relative headroom is inferred."""

    model_config = ConfigDict(frozen=True)

    continuous_db_spl: float | None = None
    peak_db_spl: float | None = None
    reference_distance_m: float = Field(gt=0.0)
    valid_frequency_domain: FrequencyDomain | None = None
    continuous_duration_s: float | None = Field(default=None, gt=0.0)
    peak_duration_s: float | None = Field(default=None, gt=0.0)
    declared_headroom_db: float | None = Field(default=None, ge=0.0)
    headroom_reference_level_db_spl: float | None = None
    provenance: EquipmentDataProvenance

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
        return _finite(value, field_name='SPL capability value')

    @model_validator(mode='after')
    def valid_level_capability(self) -> 'SplCapability':
        if (
            self.continuous_db_spl is None
            and self.peak_db_spl is None
            and self.declared_headroom_db is None
        ):
            raise ValueError('SPL capability requires at least one evidenced level datum')
        if (self.declared_headroom_db is None) != (
            self.headroom_reference_level_db_spl is None
        ):
            raise ValueError(
                'declared headroom and its reference level must be supplied together'
            )
        if (
            self.continuous_db_spl is not None
            and self.peak_db_spl is not None
            and self.peak_db_spl < self.continuous_db_spl
        ):
            raise ValueError('peak SPL must not be below continuous SPL')
        return self


class EquipmentUncertainty(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    model: Literal['bounded', 'standard_uncertainty', 'statement']
    lower: float | None = None
    upper: float | None = None
    standard_uncertainty: float | None = Field(default=None, ge=0.0)
    statement: str | None = Field(default=None, min_length=1)
    provenance: EquipmentDataProvenance

    @field_validator('lower', 'upper', 'standard_uncertainty')
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='uncertainty value')

    @model_validator(mode='after')
    def valid_uncertainty(self) -> 'EquipmentUncertainty':
        if self.model == 'bounded':
            if (
                self.lower is None
                or self.upper is None
                or self.standard_uncertainty is not None
                or self.statement is not None
            ):
                raise ValueError('bounded uncertainty requires only lower/upper')
            if self.upper < self.lower:
                raise ValueError('bounded uncertainty upper must not be below lower')
        elif self.model == 'standard_uncertainty':
            if (
                self.standard_uncertainty is None
                or self.lower is not None
                or self.upper is not None
                or self.statement is not None
            ):
                raise ValueError(
                    'standard uncertainty requires only standard_uncertainty'
                )
        else:
            if (
                self.statement is None
                or self.lower is not None
                or self.upper is not None
                or self.standard_uncertainty is not None
            ):
                raise ValueError('statement uncertainty requires only statement')
        return self


class EquipmentDefinition(BaseModel):
    """Immutable/versioned O100C physical and acoustic source definition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = EQUIPMENT_DEFINITION_SCHEMA_VERSION
    authority_version: Literal[
        'o100c-equipment-definition-1'
    ] = EQUIPMENT_DEFINITION_AUTHORITY_VERSION
    definition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    identity_kind: EquipmentIdentityKind
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)

    cabinet_envelope_m: Size3
    acoustic_reference_point_m: Offset3
    mounting: MountingMetadata = MountingMetadata()
    port: PortMetadata = PortMetadata()
    clearance: ClearanceMetadata = ClearanceMetadata()

    sensitivity: SensitivityReference | None = None
    spl_capability: SplCapability | None = None
    directivity: DirectivityCapability
    uncertainty: tuple[EquipmentUncertainty, ...] = ()

    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_definition(self) -> 'EquipmentDefinition':
        if self.identity_kind == 'manufacturer':
            if self.manufacturer is None or self.model is None:
                raise ValueError(
                    'manufacturer equipment requires manufacturer and model identity'
                )
            if not any(
                item.evidence_kind == 'manufacturer'
                for item in self.provenance
            ):
                raise ValueError(
                    'manufacturer equipment requires manufacturer provenance'
                )
        elif self.user_label is None:
            raise ValueError('user-defined equipment requires user_label')

        provenance_keys = [
            (
                item.evidence_kind,
                item.source_name,
                item.source_version,
                item.source_reference,
                item.source_sha256,
            )
            for item in self.provenance
        ]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('equipment provenance records must be unique')

        uncertainty_keys = [item.quantity for item in self.uncertainty]
        if len(uncertainty_keys) != len(set(uncertainty_keys)):
            raise ValueError('equipment uncertainty quantities must be unique')

        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('EquipmentDefinition semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'definition_id': self.definition_id,
            'version': self.version,
            'identity_kind': self.identity_kind,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'user_label': self.user_label,
            'provenance': [
                item.model_dump(mode='json')
                for item in self.provenance
            ],
            'cabinet_envelope_m': self.cabinet_envelope_m.model_dump(mode='json'),
            'acoustic_reference_point_m': (
                self.acoustic_reference_point_m.model_dump(mode='json')
            ),
            'mounting': self.mounting.model_dump(mode='json'),
            'port': self.port.model_dump(mode='json'),
            'clearance': self.clearance.model_dump(mode='json'),
            'sensitivity': (
                None
                if self.sensitivity is None
                else self.sensitivity.model_dump(mode='json')
            ),
            'spl_capability': (
                None
                if self.spl_capability is None
                else self.spl_capability.model_dump(mode='json')
            ),
            'directivity': self.directivity.model_dump(mode='json'),
            'uncertainty': [
                item.model_dump(mode='json')
                for item in self.uncertainty
            ],
        }


def build_equipment_definition(
    *,
    definition_id: str,
    version: str,
    identity_kind: EquipmentIdentityKind,
    provenance: Sequence[EquipmentDataProvenance],
    cabinet_envelope_m: Size3,
    acoustic_reference_point_m: Offset3,
    directivity: DirectivityCapability,
    manufacturer: str | None = None,
    model: str | None = None,
    user_label: str | None = None,
    mounting: MountingMetadata | None = None,
    port: PortMetadata | None = None,
    clearance: ClearanceMetadata | None = None,
    sensitivity: SensitivityReference | None = None,
    spl_capability: SplCapability | None = None,
    uncertainty: Sequence[EquipmentUncertainty] = (),
) -> EquipmentDefinition:
    provenance_items = tuple(provenance)
    uncertainty_items = tuple(uncertainty)
    mounting_value = MountingMetadata() if mounting is None else mounting
    port_value = PortMetadata() if port is None else port
    clearance_value = ClearanceMetadata() if clearance is None else clearance

    payload = {
        'schema_version': EQUIPMENT_DEFINITION_SCHEMA_VERSION,
        'authority_version': EQUIPMENT_DEFINITION_AUTHORITY_VERSION,
        'definition_id': definition_id,
        'version': version,
        'identity_kind': identity_kind,
        'manufacturer': manufacturer,
        'model': model,
        'user_label': user_label,
        'provenance': [
            item.model_dump(mode='json')
            for item in provenance_items
        ],
        'cabinet_envelope_m': cabinet_envelope_m.model_dump(mode='json'),
        'acoustic_reference_point_m': acoustic_reference_point_m.model_dump(
            mode='json'
        ),
        'mounting': mounting_value.model_dump(mode='json'),
        'port': port_value.model_dump(mode='json'),
        'clearance': clearance_value.model_dump(mode='json'),
        'sensitivity': (
            None if sensitivity is None else sensitivity.model_dump(mode='json')
        ),
        'spl_capability': (
            None
            if spl_capability is None
            else spl_capability.model_dump(mode='json')
        ),
        'directivity': directivity.model_dump(mode='json'),
        'uncertainty': [
            item.model_dump(mode='json')
            for item in uncertainty_items
        ],
    }
    return EquipmentDefinition(
        definition_id=definition_id,
        version=version,
        identity_kind=identity_kind,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        provenance=provenance_items,
        cabinet_envelope_m=cabinet_envelope_m,
        acoustic_reference_point_m=acoustic_reference_point_m,
        mounting=mounting_value,
        port=port_value,
        clearance=clearance_value,
        sensitivity=sensitivity,
        spl_capability=spl_capability,
        directivity=directivity,
        uncertainty=uncertainty_items,
        semantic_sha256=_digest(payload),
    )


class EquipmentCapabilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim: EquipmentCapabilityClaim
    decision: EquipmentCapabilityDecision
    reasons: tuple[str, ...] = Field(min_length=1)


def evaluate_equipment_capability(
    definition: EquipmentDefinition,
    claim: EquipmentCapabilityClaim,
    *,
    frequency_hz: float | None = None,
    horizontal_angle_deg: float | None = None,
    vertical_angle_deg: float | None = None,
) -> EquipmentCapabilityResult:
    """Fail-closed source/equipment capability gate for downstream objectives."""

    reasons: list[str] = []

    if claim == 'cabinet_geometry':
        return EquipmentCapabilityResult(
            claim=claim,
            decision='SUPPORTED',
            reasons=('explicit cabinet envelope and acoustic reference point are present',),
        )

    if claim == 'sensitivity_reference':
        if definition.sensitivity is None:
            reasons.append('sensitivity/reference level is not evidenced')
        else:
            return EquipmentCapabilityResult(
                claim=claim,
                decision='SUPPORTED',
                reasons=('explicit sensitivity/reference conditions are present',),
            )

    elif claim == 'continuous_spl':
        if (
            definition.spl_capability is None
            or definition.spl_capability.continuous_db_spl is None
        ):
            reasons.append('continuous SPL capability is not evidenced')
        else:
            return EquipmentCapabilityResult(
                claim=claim,
                decision='SUPPORTED',
                reasons=('explicit continuous SPL capability is present',),
            )

    elif claim == 'peak_spl':
        if (
            definition.spl_capability is None
            or definition.spl_capability.peak_db_spl is None
        ):
            reasons.append('peak SPL capability is not evidenced')
        else:
            return EquipmentCapabilityResult(
                claim=claim,
                decision='SUPPORTED',
                reasons=('explicit peak SPL capability is present',),
            )

    else:
        directivity = definition.directivity
        tier = directivity.tier
        tier_supported = {
            'directivity_summary': tier != 'unknown',
            'directivity_magnitude': tier in {
                'complex',
                'magnitude_only',
                'analytic',
            },
            'directivity_complex': tier == 'complex'
            or (tier == 'analytic' and directivity.coherent_phase),
            'coherent_phase': tier == 'complex'
            or (tier == 'analytic' and directivity.coherent_phase),
        }[claim]
        if not tier_supported:
            reasons.append(
                f'directivity tier {tier!r} does not support {claim!r}'
            )
        else:
            domain = directivity.valid_domain
            if domain is None:
                reasons.append('directivity valid domain is unavailable')
            else:
                if frequency_hz is not None and not domain.frequency.contains(
                    frequency_hz
                ):
                    reasons.append('requested frequency is outside directivity domain')
                if (
                    horizontal_angle_deg is not None
                    and not domain.horizontal.contains(horizontal_angle_deg)
                ):
                    reasons.append(
                        'requested horizontal angle is outside directivity domain'
                    )
                if (
                    vertical_angle_deg is not None
                    and not domain.vertical.contains(vertical_angle_deg)
                ):
                    reasons.append(
                        'requested vertical angle is outside directivity domain'
                    )
                if not reasons:
                    return EquipmentCapabilityResult(
                        claim=claim,
                        decision='SUPPORTED',
                        reasons=(
                            'explicit directivity capability and requested domain are supported',
                        ),
                    )

    return EquipmentCapabilityResult(
        claim=claim,
        decision='UNSUPPORTED',
        reasons=tuple(reasons),
    )


def equipment_capability_matrix(
    definition: EquipmentDefinition,
    *,
    frequency_hz: float | None = None,
    horizontal_angle_deg: float | None = None,
    vertical_angle_deg: float | None = None,
) -> tuple[EquipmentCapabilityResult, ...]:
    claims: tuple[EquipmentCapabilityClaim, ...] = (
        'cabinet_geometry',
        'sensitivity_reference',
        'continuous_spl',
        'peak_spl',
        'directivity_summary',
        'directivity_magnitude',
        'directivity_complex',
        'coherent_phase',
    )
    return tuple(
        evaluate_equipment_capability(
            definition,
            claim,
            frequency_hz=frequency_hz,
            horizontal_angle_deg=horizontal_angle_deg,
            vertical_angle_deg=vertical_angle_deg,
        )
        for claim in claims
    )
