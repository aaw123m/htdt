"""Structure-borne excitation and spatial-remapping authority
(issues #653, #664).

Airborne sound isolation does not block mechanical vibration
injected directly into the building structure — subwoofers,
cabinets, hush-box fans, racks and tactile transducers can excite
adjacent rooms through solid paths (#653, ISO 10848-1 flanking).
And verifying speaker identity/geometry does not equal compensating
spatial rendering when installed positions differ from the
reference layout (#664 — remapping needs measured actual positions,
as current Trinnov 2D/3D Remapping does).

Basis: ISO 10848-1:2017 (flanking transmission); Trinnov
Optimizer/Altitude remapping documentation.
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


class StructurebornePath(BaseModel):
    """Declared solid-path vibration excitation (#653) — source,
    mount/coupling, receiving structure. Airborne-isolation
    qualification does not cover this path."""

    model_config = ConfigDict(frozen=True)

    path_id: str
    path_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    source_kind: Literal[
        'subwoofer', 'loudspeaker_cabinet', 'fan_hushbox',
        'rack_equipment', 'tactile_transducer', 'other', 'unknown',
    ]
    mount_kind: Literal[
        'isolated', 'rigid_coupled', 'floating_floor',
        'wall_mounted', 'unknown',
    ]
    receiving_room: str | None = None
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('source_kind') not in (
                'subwoofer', 'loudspeaker_cabinet', 'fan_hushbox',
                'rack_equipment', 'tactile_transducer', 'other',
                'unknown',
            ):
                raise ValueError('unknown source kind')
            if data.get('source_kind') == 'unknown':
                raise ValueError(
                    'a structure-borne path requires a declared '
                    'source'
                )
            if data.get('mount_kind') not in (
                'isolated', 'rigid_coupled', 'floating_floor',
                'wall_mounted', 'unknown',
            ):
                raise ValueError('unknown mount kind')
            if data.get('mount_kind') == 'unknown':
                raise ValueError(
                    'declare the mount/coupling — isolation vs '
                    'rigid coupling changes the path'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'path_id', 'path_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'StructurebornePath':
        return _seal(
            cls, payload, 'path_id', 'path_sha256', 'sbp'
        )


class SpatialRemappingEvidence(BaseModel):
    """Actual-position spatial-remap qualification (#664) — measured
    speaker positions vs reference layout, declared remap mode and
    the verification that phantom images land at intended
    locations."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    remap_mode: Literal[
        '2d_remapping', '3d_remapping', 'automatic_routing',
        'none_declared', 'other', 'unknown',
    ]
    actual_positions_ref: AuthorityRef | None = None
    reference_layout_ref: AuthorityRef | None = None
    verification_ref: AuthorityRef | None = None
    speakers_remapped: int | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('remap_mode') not in (
                '2d_remapping', '3d_remapping', 'automatic_routing',
                'none_declared', 'other', 'unknown',
            ):
                raise ValueError('unknown remap mode')
            if data.get('remap_mode') == 'unknown':
                raise ValueError(
                    'spatial remapping requires a declared mode'
                )
            if data.get('remap_mode') in (
                '2d_remapping', '3d_remapping'
            ) and data.get('actual_positions_ref') is None:
                raise ValueError(
                    'remapping requires measured actual speaker '
                    'positions — it cannot compensate from the '
                    'ideal layout alone'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SpatialRemappingEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'srm'
        )


StructureborneVerdict = Literal[
    'qualified_path',
    'airborne_is_not_structureborne',
    'remap_unverified',
    'measured_positions_required',
]


def evaluate_structureborne_claim(
    path: StructurebornePath | None,
    remap: SpatialRemappingEvidence | None,
    *,
    airborne_isolation_qualified: bool = False,
) -> tuple[StructureborneVerdict, str]:
    """Judge structure-borne / remapping claims (#653/#664)."""
    if path is None:
        if airborne_isolation_qualified:
            return (
                'airborne_is_not_structureborne',
                'airborne isolation does not cover mechanical '
                'injection into the building structure',
            )
        return (
            'airborne_is_not_structureborne',
            'no structure-borne path declared',
        )
    if path.mount_kind == 'rigid_coupled' and (
        path.measurement_ref is None
    ):
        return (
            'airborne_is_not_structureborne',
            'rigidly coupled source with no vibration evidence — '
            'adjacent-room excitation unbounded',
        )
    if remap is not None:
        if remap.remap_mode in ('2d_remapping', '3d_remapping') and (
            remap.verification_ref is None
        ):
            return (
                'remap_unverified',
                'remap mode declared but phantom-image landing '
                'unverified',
            )
        if remap.remap_mode == 'none_declared':
            return (
                'measured_positions_required',
                'installed positions differ from reference — no '
                'remap compensation declared',
            )
    return (
        'qualified_path',
        'solid-path excitation declared/bounded and remap '
        'qualified',
    )


STRUCTUREBORNE_LABELS: dict[str, str] = {
    'qualified_path': '固体伝搬適格',
    'airborne_is_not_structureborne': '空気伝搬遮断は固体伝搬ではない',
    'remap_unverified': 'リマップ未検証',
    'measured_positions_required': '実測位置必須',
}
