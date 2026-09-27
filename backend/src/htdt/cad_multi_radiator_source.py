"""Multi-radiator loudspeaker source authority (#1006).

A real loudspeaker is almost never one source: separate drivers, ports,
passive radiators and distributed apertures radiate from different
positions, over different bands, with different phase behaviour. The
single ``acoustic_reference_point_m`` on an equipment definition is a
valid *geometric* datum but cannot answer where the tweeter sits relative
to the port, or whether the two can be coherently summed.

This module models the radiating elements as first-class records:

- :class:`SourceRadiatorElement` — one radiating element with its kind
  (driver, compression driver, port, passive radiator, distributed
  aperture, equivalent point, opaque), local position/orientation in the
  enclosure frame, valid frequency band and its transfer evidence
  (``complex``, ``magnitude_only``, ``declared``, ``none``);
- :class:`SourceTransferEdge` — the crossover/combining record between
  the shared input and each element (gain, delay, polarity, filter refs);
- :class:`SourceReferencePoints` — the distinct reference frames the
  module refuses to conflate: the enclosure *geometric reference*, the
  measurement *dataset phase origin*, the *effective acoustic center* and
  the *solver-equivalent point*. Each is optional and separately
  provenanced; an absent frame is ``None``, not a borrow from another;
- :class:`MultiRadiatorSourceModel` — the versioned, hashed assembly of
  elements + transfer edges + reference points.

:func:`evaluate_multi_radiator_coherence` reports, per model, whether
coherent summation is supportable: every element must carry complex
transfer evidence sharing one dataset phase origin. Magnitude-only or
undeclared transfer evidence can never be promoted to coherent summation.
A whole-system measured directivity reference stays valid on its own
record — the model never forces a decomposition of a system measured as
one.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_bass_management import FrequencyBand
from .cad_equipment import EquipmentDataProvenance
from .cad_scene import Offset3
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


MULTI_RADIATOR_SOURCE_AUTHORITY_VERSION = 'multi-radiator-source-1'

#: Physical/abstract kind of one radiating element.
RadiatorElementKind = Literal[
    'piston',
    'driver',
    'compression_driver',
    'port',
    'passive_radiator',
    'distributed_aperture',
    'equivalent_point',
    'opaque',
]

#: What an element's transfer to the far field is backed by.
RadiatorTransferEvidence = Literal[
    'complex', 'magnitude_only', 'declared', 'none'
]






def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class SourceRadiatorElement(BaseModel):
    """One radiating element of a loudspeaker.

    ``local_position_m`` is in the enclosure frame (the model's
    ``geometric_reference_m`` frame), never a world position. ``valid_band``
    bounds where the element radiates; ``transfer_evidence`` records what
    phase/complex data exists — it is the gate on coherent summation.
    """

    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1)
    kind: RadiatorElementKind
    local_position_m: Offset3
    local_orientation_deg: Offset3 | None = None
    valid_band: FrequencyBand | None = None
    transfer_evidence: RadiatorTransferEvidence = 'none'
    #: Reference to a transfer dataset authority id carrying the element's
    #: complex (or magnitude-only) transfer to a stated point.
    transfer_dataset_ref: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_element(self) -> 'SourceRadiatorElement':
        if self.transfer_evidence == 'none' and (
            self.transfer_dataset_ref is not None
        ):
            raise ValueError(
                'a transfer dataset ref requires declared evidence kind'
            )
        if self.transfer_evidence == 'complex' and (
            self.transfer_dataset_ref is None
        ):
            raise ValueError(
                'complex transfer evidence requires a dataset reference'
            )
        return self


class SourceTransferEdge(BaseModel):
    """Input→element transfer (the crossover/combining record)."""

    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1)
    gain_db: float | None = None
    delay_ms: float | None = Field(default=None, ge=0.0)
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'
    filter_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def finite_values(self) -> 'SourceTransferEdge':
        for name in ('gain_db', 'delay_ms'):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        return self


class SourceReferencePoints(BaseModel):
    """The distinct reference frames a multi-radiator model may carry.

    All four are separate records — the *geometric* enclosure reference,
    the *dataset phase origin* a measured dataset is expressed about, the
    *effective acoustic center* (where the system approximates a point
    source when that is evidenced), and the *solver-equivalent point* a
    solver actually places. None defaults to another; each may be absent.
    """

    model_config = ConfigDict(frozen=True)

    geometric_reference_m: Offset3 | None = None
    dataset_phase_origin_m: Offset3 | None = None
    dataset_phase_origin_ref: str | None = Field(default=None, min_length=1)
    effective_acoustic_center_m: Offset3 | None = None
    effective_acoustic_center_ref: str | None = Field(
        default=None, min_length=1
    )
    solver_equivalent_point_m: Offset3 | None = None

    @model_validator(mode='after')
    def valid_refs(self) -> 'SourceReferencePoints':
        if (self.dataset_phase_origin_m is not None) != (
            self.dataset_phase_origin_ref is not None
        ):
            raise ValueError(
                'dataset phase origin and its authority ref come together'
            )
        if (self.effective_acoustic_center_m is not None) != (
            self.effective_acoustic_center_ref is not None
        ):
            raise ValueError(
                'effective acoustic center and its authority ref come '
                'together'
            )
        return self


class MultiRadiatorSourceModel(BaseModel):
    """Versioned multi-element source assembly for one equipment definition.

    The model supplements — never replaces — the equipment definition's
    ``acoustic_reference_point_m``: that field remains the geometric
    placement datum, while this model carries the per-element geometry and
    transfer lineage needed for coherent source modelling. A
    ``whole_system_directivity_ref`` records that the system was measured
    as one — a valid, separate evidence path that is never forced into a
    per-element decomposition.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'multi-radiator-source-1'
    ] = MULTI_RADIATOR_SOURCE_AUTHORITY_VERSION
    model_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    elements: tuple[SourceRadiatorElement, ...] = ()
    transfer_edges: tuple[SourceTransferEdge, ...] = ()
    reference_points: SourceReferencePoints = Field(
        default_factory=SourceReferencePoints
    )
    #: Measured whole-system directivity authority (e.g. a resolved
    #: directivity dataset id) — stays valid independently.
    whole_system_directivity_ref: str | None = Field(
        default=None, min_length=1
    )
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_model(self) -> 'MultiRadiatorSourceModel':
        element_ids = [element.element_id for element in self.elements]
        if len(set(element_ids)) != len(element_ids):
            raise ValueError('duplicate radiator element ids')
        known = set(element_ids)
        for edge in self.transfer_edges:
            if edge.element_id not in known:
                raise ValueError(
                    f'transfer edge references unknown element '
                    f'{edge.element_id}'
                )
        edge_ids = [edge.element_id for edge in self.transfer_edges]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError('duplicate transfer edge for one element')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'MultiRadiatorSourceModel semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'model_id': self.model_id,
            'version': self.version,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'elements': [
                element.model_dump(mode='json') for element in self.elements
            ],
            'transfer_edges': [
                edge.model_dump(mode='json') for edge in self.transfer_edges
            ],
            'reference_points': self.reference_points.model_dump(mode='json'),
            'whole_system_directivity_ref': self.whole_system_directivity_ref,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_multi_radiator_source_model(
    *,
    model_id: str | None = None,
    version: str = '1',
    equipment_definition_id: str,
    equipment_definition_version: str,
    equipment_definition_sha256: str,
    elements: tuple[SourceRadiatorElement, ...] = (),
    transfer_edges: tuple[SourceTransferEdge, ...] = (),
    reference_points: SourceReferencePoints | None = None,
    whole_system_directivity_ref: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> MultiRadiatorSourceModel:
    payload: dict[str, Any] = {
        'authority_version': MULTI_RADIATOR_SOURCE_AUTHORITY_VERSION,
        'model_id': model_id or str(uuid4()),
        'version': version,
        'equipment_definition_id': equipment_definition_id,
        'equipment_definition_version': equipment_definition_version,
        'equipment_definition_sha256': equipment_definition_sha256,
        'elements': elements,
        'transfer_edges': transfer_edges,
        'reference_points': (
            reference_points
            if reference_points is not None
            else SourceReferencePoints()
        ),
        'whole_system_directivity_ref': whole_system_directivity_ref,
        'provenance': provenance,
    }
    provisional = MultiRadiatorSourceModel.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return MultiRadiatorSourceModel(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class RadiatorCoherenceCheck(BaseModel):
    """One coherence check on a multi-radiator model."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class RadiatorCoherenceEvaluation(BaseModel):
    """Whether the model can support coherent element summation."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    model_id: str
    model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[RadiatorCoherenceCheck, ...]
    coherent_summation_supported: bool
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'RadiatorCoherenceEvaluation':
        if self.evaluation_sha256 != _hash(self.identity_payload()):
            raise ValueError('radiator coherence evaluation hash mismatch')
        expected = 'rce-' + self.evaluation_sha256[:24]
        if self.evaluation_id != expected:
            raise ValueError('radiator coherence evaluation id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': MULTI_RADIATOR_SOURCE_AUTHORITY_VERSION,
            'model_id': self.model_id,
            'model_sha256': self.model_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
            'coherent_summation_supported': self.coherent_summation_supported,
        }


def evaluate_multi_radiator_coherence(
    *, model: MultiRadiatorSourceModel
) -> RadiatorCoherenceEvaluation:
    """Report whether the model supports coherent element summation.

    - ``elements_recorded``: at least two radiating elements are modelled
      (a single element is a monopole record, not a multi-radiator model);
    - ``element_bands_declared``: every element carries its valid band —
      overlapping bands without declared extents cannot be de-overlapped;
    - ``transfer_evidence_complex``: every element carries ``complex``
      transfer evidence bound to a dataset — magnitude-only or
      undeclared evidence can never be promoted to coherent summation;
    - ``phase_origin_shared``: a single dataset phase origin is recorded;
      per-element phase origins are not mixable;
    - ``double_counting_guard``: band-overlapping elements without a
      transfer edge are flagged — the same energy must not be counted
      twice;
    - ``whole_system_directivity_valid``: a bound whole-system
      directivity record stays valid evidence on its own.
    """

    checks: list[RadiatorCoherenceCheck] = []
    elements = model.elements

    checks.append(
        RadiatorCoherenceCheck(
            check='elements_recorded',
            status='PASS' if len(elements) >= 2 else 'UNKNOWN',
            reason=(
                f'{len(elements)} radiating elements recorded'
                if len(elements) >= 2
                else 'fewer than two elements — not a multi-radiator model'
            ),
        )
    )

    undeclared_bands = [e.element_id for e in elements if e.valid_band is None]
    checks.append(
        RadiatorCoherenceCheck(
            check='element_bands_declared',
            status='PASS' if not undeclared_bands else 'UNKNOWN',
            reason=(
                'every element carries a valid band'
                if not undeclared_bands
                else 'elements without declared band: '
                + ', '.join(undeclared_bands)
            ),
        )
    )

    non_complex = [
        e.element_id for e in elements if e.transfer_evidence != 'complex'
    ]
    checks.append(
        RadiatorCoherenceCheck(
            check='transfer_evidence_complex',
            status='PASS' if elements and not non_complex else 'UNKNOWN',
            reason=(
                'all elements carry complex transfer evidence'
                if elements and not non_complex
                else (
                    'non-complex transfer evidence on: '
                    + ', '.join(non_complex)
                    if non_complex
                    else 'no elements recorded'
                )
            ),
        )
    )

    phase_origin = model.reference_points.dataset_phase_origin_m
    checks.append(
        RadiatorCoherenceCheck(
            check='phase_origin_shared',
            status='PASS' if phase_origin is not None else 'UNKNOWN',
            reason=(
                'shared dataset phase origin recorded'
                if phase_origin is not None
                else 'no shared dataset phase origin — element phase '
                'references cannot be mixed'
            ),
        )
    )

    edge_ids = {edge.element_id for edge in model.transfer_edges}
    overlap_pairs: list[str] = []
    element_list = list(elements)
    for index, element in enumerate(element_list):
        if element.valid_band is None:
            continue
        for other in element_list[index + 1:]:
            if other.valid_band is None:
                continue
            if (
                element.valid_band.low_hz < other.valid_band.high_hz
                and other.valid_band.low_hz < element.valid_band.high_hz
            ):
                uncovered = [
                    e.element_id
                    for e in (element, other)
                    if e.element_id not in edge_ids
                ]
                if uncovered:
                    overlap_pairs.append(
                        f'{element.element_id}~{other.element_id}'
                    )
    checks.append(
        RadiatorCoherenceCheck(
            check='double_counting_guard',
            status='PASS' if not overlap_pairs else 'UNKNOWN',
            reason=(
                'overlapping bands all carry transfer edges'
                if not overlap_pairs
                else 'band-overlapping elements lacking crossover record: '
                + ', '.join(overlap_pairs)
            ),
        )
    )

    checks.append(
        RadiatorCoherenceCheck(
            check='whole_system_directivity_valid',
            status=(
                'PASS'
                if model.whole_system_directivity_ref is not None
                else 'NOT_APPLICABLE'
            ),
            reason=(
                'whole-system measured directivity bound and valid '
                'independently'
                if model.whole_system_directivity_ref is not None
                else 'no whole-system directivity record bound'
            ),
        )
    )

    supported = (
        all(
            check.status == 'PASS'
            for check in checks
            if check.check != 'whole_system_directivity_valid'
        )
    )
    probe = RadiatorCoherenceEvaluation.model_construct(
        evaluation_id='',
        model_id=model.model_id,
        model_sha256=model.semantic_sha256,
        checks=tuple(checks),
        coherent_summation_supported=supported,
        evaluation_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return RadiatorCoherenceEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='rce-' + digest[:24],
        evaluation_sha256=digest,
    )


__all__ = [
    'MULTI_RADIATOR_SOURCE_AUTHORITY_VERSION',
    'MultiRadiatorSourceModel',
    'RadiatorCoherenceCheck',
    'RadiatorCoherenceEvaluation',
    'RadiatorElementKind',
    'RadiatorTransferEvidence',
    'SourceRadiatorElement',
    'SourceReferencePoints',
    'SourceTransferEdge',
    'build_multi_radiator_source_model',
    'evaluate_multi_radiator_coherence',
]
