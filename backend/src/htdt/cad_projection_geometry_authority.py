"""Projection image-geometry / masking qualification (#622, REV57-PROJ).

An image can have correct center color/luminance and correct nominal
screen size while still presenting content with the wrong crop, aspect,
geometric distortion, masking or lens-memory alignment. This module owns
installed image-geometry evidence:

- :class:`CadPresentationGeometryBinding` — the sealed identity of one
  presentation profile: content/projected raster, screen, masking,
  lens memory, zoom/shift, anamorphic and processor state, signal
  profile. A mode/profile change is a different geometry qualification.
- :class:`CadRasterStageDeclaration` — one stage of the content→screen
  raster chain (source active raster, processed output raster,
  projector imaging raster, projected image boundary, visible/masked
  screen boundary) so where crop/scale happens stays attributable.
- :class:`CadImageGeometryMeasurement` — one sealed measurement
  campaign: method, exact test-pattern identity, per-quantity readings
  (aspect, scale, crop, offset, keystone, warp, masking overlap), the
  observed physical-vs-digital correction state and per-edge crop
  sources.
- :class:`CadLensMemoryRecallRecord` — lens-memory repeatability
  evidence: a saved preset name is not proof the image returned to the
  same boundary.
- :class:`CadGeometryEvaluation` + :func:`evaluate_presentation_geometry`
  — the fail-closed verdict. Digital warp/keystone correcting a
  misaligned physical pose reports ``verified_with_digital_correction``
  with its raster/latency costs, never a silent ``verified``.

Honesty rules baked in:

- Source raster, processed raster, projected boundary and visible
  screen boundary are independent authorities — a correct mask ratio
  does not excuse an upstream stretch/crop.
- No geometry judgment is canonical without an exact test-pattern
  identity (#608/#295 bound).
- Physical projector pose and digital geometry correction are never
  conflated: digital correction stays explicit and its consequences
  (raster/resolution loss, scaling, latency) are reported.
- Anamorphic lens state and electronic stretch must be consistent;
  stretch with the lens out of the path fails explicitly.
- Masking state is measured separately from the projected image — a
  correct lens memory beside an intruding mask is a fail.

Literature basis
----------------
- AVIXA Systems Performance Verification Guide — completed displays are
  verified for correct image geometry and freedom from stretching,
  keystone and barrel/pincushion distortion; physical projection
  alignment is a prerequisite, and mapped/processed systems may need
  checks from multiple viewer positions.
- SMPTE projector alignment / screen image quality RP lineage
  (e.g. RP 40 stable 2016): separate checks for projectable image area,
  masking, alignment, focus, anamorphic geometry.
- SMPTE DPROVE practice — digital projector performance, alignment,
  masking and picture-sound synchronization checks.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


GEOMETRY_AUTHORITY_SCHEMA_VERSION = 'proj-geometry-1'
GEOMETRY_EVALUATION_VERSION = 'proj-geometry-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#622)
# ---------------------------------------------------------------------------

RasterStage = Literal[
    'source_active_raster',
    'processed_output_raster',
    'projector_imaging_raster',
    'projected_image_boundary',
    'visible_screen_boundary',
]
"""The content→screen raster chain. Missing stages stay absent — an
unverified stage is never assumed clean."""

RasterStageSource = Literal['declared', 'measured', 'readback', 'unknown']

GeometryMethod = Literal[
    'manual_grid_visual_inspection',
    'camera_based_image_geometry',
    'surveyed_screen_camera',
    'projector_processor_readback',
    'service_tool',
    'other_validated_method',
]

GeometryQuantity = Literal[
    'aspect_ratio',
    'horizontal_scale',
    'vertical_scale',
    'center_offset',
    'rotation',
    'corner_error',
    'keystone_trapezoid',
    'barrel_pincushion',
    'edge_bow_warp',
    'crop_per_edge',
    'masking_overlap_gap',
]

ALL_GEOMETRY_QUANTITIES: tuple[GeometryQuantity, ...] = (
    'aspect_ratio',
    'horizontal_scale',
    'vertical_scale',
    'center_offset',
    'rotation',
    'corner_error',
    'keystone_trapezoid',
    'barrel_pincushion',
    'edge_bow_warp',
    'crop_per_edge',
    'masking_overlap_gap',
)

ImageEdge = Literal['top', 'bottom', 'left', 'right']

CropSource = Literal[
    'source_crop',
    'processor_crop',
    'projector_overscan',
    'masking_crop',
    'off_screen_loss',
    'unknown',
]
"""Per-edge crop attribution — black bars are not proof nothing was
lost, and processor/projector/mask/off-screen causes stay distinct."""

CorrectionKind = Literal[
    'physical_projector_pose',
    'lens_shift_zoom',
    'optical_anamorphic',
    'digital_keystone',
    'warp_geometric_processing',
    'scaling_crop',
]
"""Optical alignment vs digital correction — kept strictly separate."""

AnamorphicState = Literal[
    'none',
    'lens_inserted',
    'lens_bypassed',
    'lens_removed',
    'electronic_stretch',
    'unknown',
]

PhysicalAlignment = Literal[
    'physically_aligned',
    'physically_misaligned',
    'unknown',
]

DigitalCorrectionState = Literal['none', 'active', 'unknown']

GeometryQuantityState = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']

GeometryVerdict = Literal[
    'verified',
    'verified_with_limitations',
    'verified_with_digital_correction',
    'failed',
    'insufficient_evidence',
]


# ---------------------------------------------------------------------------
# Presentation-geometry binding
# ---------------------------------------------------------------------------


class CadRasterStageDeclaration(BaseModel):
    """One stage of the raster chain.

    ``width_units``/``height_units`` are in the stage's own units
    (pixels for rasters, metres or pixels for boundaries — declared in
    ``units``). A stage that was not measured stays absent from the
    measurement rather than being filled with a guess.
    """

    model_config = ConfigDict(frozen=True)

    stage: RasterStage
    width: float | None = None
    height: float | None = None
    units: str | None = None
    source: RasterStageSource = 'unknown'

    @model_validator(mode='after')
    def valid_stage(self) -> 'CadRasterStageDeclaration':
        for label, value in (
            ('width', self.width), ('height', self.height),
        ):
            if value is not None:
                _require_finite(value, f'raster stage {label}')
                if value <= 0:
                    raise ValueError(f'raster stage {label} must be positive')
        if (self.width is None) != (self.height is None):
            raise ValueError(
                'a raster stage declares both dimensions or neither'
            )
        if self.width is not None and self.units is None:
            raise ValueError('raster stage dimensions require units')
        return self


class CadPresentationGeometryBinding(BaseModel):
    """Sealed identity of one presentation profile's geometry claim.

    Binds the exact content raster, projected profile (#565), screen
    (#613/#282), projector (#285), lens memory / zoom / shift /
    anamorphic / keystone / warp / processor state and signal profile
    (#583). Any change produces a different binding hash — a prior
    geometry qualification never silently transfers.
    """

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    presentation_profile_ref: AuthorityRef | None = None
    screen_ref: AuthorityRef | None = None
    projector_ref: AuthorityRef | None = None
    signal_profile_ref: AuthorityRef | None = None
    content_aspect: float | None = None
    projected_aspect: float | None = None
    lens_memory_id: str | None = None
    zoom_ratio: float | None = None
    lens_shift_h: float | None = None
    lens_shift_v: float | None = None
    keystone_state: DigitalCorrectionState = 'unknown'
    warp_state: DigitalCorrectionState = 'unknown'
    anamorphic_state: AnamorphicState = 'unknown'
    processor_scaling_mode: str | None = None
    masking_state: str | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_binding(self) -> 'CadPresentationGeometryBinding':
        _require_iso8601(
            self.declared_at_utc, 'binding declared_at_utc'
        )
        for label, value in (
            ('content_aspect', self.content_aspect),
            ('projected_aspect', self.projected_aspect),
            ('zoom_ratio', self.zoom_ratio),
            ('lens_shift_h', self.lens_shift_h),
            ('lens_shift_v', self.lens_shift_v),
        ):
            if value is not None:
                _require_finite(value, f'geometry binding {label}')
        for label, value in (
            ('content_aspect', self.content_aspect),
            ('projected_aspect', self.projected_aspect),
            ('zoom_ratio', self.zoom_ratio),
        ):
            if value is not None and value <= 0:
                raise ValueError(f'geometry binding {label} must be positive')
        for ref, label in (
            (self.presentation_profile_ref, 'presentation_profile_ref'),
            (self.screen_ref, 'screen_ref'),
            (self.projector_ref, 'projector_ref'),
            (self.signal_profile_ref, 'signal_profile_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.binding_sha256 != expected:
            raise ValueError('geometry binding hash mismatch')
        if self.binding_id != _semantic_id('geobind', expected):
            raise ValueError('binding id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'presentation_profile_ref': (
                self.presentation_profile_ref.model_dump(mode='json')
                if self.presentation_profile_ref is not None else None
            ),
            'screen_ref': (
                self.screen_ref.model_dump(mode='json')
                if self.screen_ref is not None else None
            ),
            'projector_ref': (
                self.projector_ref.model_dump(mode='json')
                if self.projector_ref is not None else None
            ),
            'signal_profile_ref': (
                self.signal_profile_ref.model_dump(mode='json')
                if self.signal_profile_ref is not None else None
            ),
            'content_aspect': self.content_aspect,
            'projected_aspect': self.projected_aspect,
            'lens_memory_id': self.lens_memory_id,
            'zoom_ratio': self.zoom_ratio,
            'lens_shift_h': self.lens_shift_h,
            'lens_shift_v': self.lens_shift_v,
            'keystone_state': self.keystone_state,
            'warp_state': self.warp_state,
            'anamorphic_state': self.anamorphic_state,
            'processor_scaling_mode': self.processor_scaling_mode,
            'masking_state': self.masking_state,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def digital_correction_active(self) -> bool:
        return self.keystone_state == 'active' or self.warp_state == 'active'


def geometry_binding_ref(
    binding: CadPresentationGeometryBinding,
) -> AuthorityRef:
    return AuthorityRef(
        kind='presentation_geometry_binding',
        ref_id=binding.binding_id,
        ref_sha256=binding.binding_sha256,
    )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class CadGeometryQuantityReading(BaseModel):
    """One measured geometry quantity.

    ``value`` is the measured figure in ``units`` (pixels, mm, degrees,
    ratio — per the method/profile); ``edge`` names the edge for
    ``crop_per_edge``/``masking_overlap_gap`` readings. ``observed_state``
    is an inspector-declared PASS/FAIL where no numeric tolerance
    applies; it never substitutes for measured values when they exist.
    """

    model_config = ConfigDict(frozen=True)

    quantity: GeometryQuantity
    edge: ImageEdge | None = None
    value: float | None = None
    units: str | None = None
    observed_state: GeometryQuantityState | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_reading(self) -> 'CadGeometryQuantityReading':
        if self.value is not None:
            _require_finite(self.value, 'geometry reading value')
            if self.units is None:
                raise ValueError(
                    'a measured geometry value requires its units'
                )
        if self.value is None and self.observed_state is None:
            raise ValueError(
                'a geometry reading requires a measured value or an '
                'observed state — an empty reading is not evidence'
            )
        return self


class CadCropAttribution(BaseModel):
    """Per-edge crop source — keeps processor/projector/mask/off-screen
    causes distinct instead of assuming black bars mean no loss."""

    model_config = ConfigDict(frozen=True)

    edge: ImageEdge
    source: CropSource
    amount: float | None = None
    units: str | None = None

    @model_validator(mode='after')
    def valid_attribution(self) -> 'CadCropAttribution':
        if self.amount is not None:
            _require_finite(self.amount, 'crop amount')
            if self.amount < 0:
                raise ValueError('crop amount must be non-negative')
            if self.units is None:
                raise ValueError('crop amount requires units')
        return self


class CadMaskingEdgeState(BaseModel):
    """One observed masking edge vs the projected image boundary."""

    model_config = ConfigDict(frozen=True)

    edge: ImageEdge
    overlap_m: float | None = None
    gap_m: float | None = None
    observed_state: GeometryQuantityState | None = None

    @model_validator(mode='after')
    def valid_masking_edge(self) -> 'CadMaskingEdgeState':
        for label, value in (
            ('overlap_m', self.overlap_m), ('gap_m', self.gap_m),
        ):
            if value is not None:
                _require_finite(value, f'masking edge {label}')
                if value < 0:
                    raise ValueError(
                        f'masking edge {label} must be non-negative'
                    )
        return self


class CadImageGeometryMeasurement(BaseModel):
    """One sealed geometry measurement campaign against a binding.

    ``test_pattern_identity`` is required — geometry judgments from an
    arbitrary movie frame are not canonical evidence. The method keeps
    its viewpoint/sensor/uncertainty so a camera-derived result cannot
    claim subpixel precision without calibration evidence.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    binding_ref: AuthorityRef
    method: GeometryMethod
    test_pattern_identity: str = Field(min_length=1)
    test_pattern_ref: AuthorityRef | None = None
    viewpoint: str | None = None
    sensor_identity: str | None = None
    calibration_ref: str | None = None
    uncertainty: float | None = None
    readings: tuple[CadGeometryQuantityReading, ...] = ()
    raster_chain: tuple[CadRasterStageDeclaration, ...] = ()
    crop_attributions: tuple[CadCropAttribution, ...] = ()
    masking_edges: tuple[CadMaskingEdgeState, ...] = ()
    physical_alignment: PhysicalAlignment = 'unknown'
    active_corrections: tuple[CorrectionKind, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'CadImageGeometryMeasurement':
        _require_iso8601(
            self.observed_at_utc, 'measurement observed_at_utc'
        )
        if self.binding_ref.ref_sha256 is None:
            raise ValueError('measurements must pin the binding sha256')
        if self.test_pattern_ref is not None and (
            self.test_pattern_ref.ref_sha256 is None
        ):
            raise ValueError('test pattern ref must pin its sha256')
        if self.uncertainty is not None:
            _require_finite(self.uncertainty, 'measurement uncertainty')
            if self.uncertainty < 0:
                raise ValueError('uncertainty must be non-negative')
        seen_stages = [s.stage for s in self.raster_chain]
        if len(seen_stages) != len(set(seen_stages)):
            raise ValueError('raster chain stages must be unique')
        seen_pairs = [
            (r.quantity, r.edge) for r in self.readings
        ]
        if len(seen_pairs) != len(set(seen_pairs)):
            raise ValueError(
                'duplicate (quantity, edge) geometry readings'
            )
        seen_edges = [c.edge for c in self.crop_attributions]
        if len(seen_edges) != len(set(seen_edges)):
            raise ValueError('crop attributions must be unique per edge')
        mask_edges = [m.edge for m in self.masking_edges]
        if len(mask_edges) != len(set(mask_edges)):
            raise ValueError('masking edges must be unique per edge')
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('geometry measurement hash mismatch')
        if self.measurement_id != _semantic_id('geomeas', expected):
            raise ValueError('measurement id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'binding_ref': self.binding_ref.model_dump(mode='json'),
            'method': self.method,
            'test_pattern_identity': self.test_pattern_identity,
            'test_pattern_ref': (
                self.test_pattern_ref.model_dump(mode='json')
                if self.test_pattern_ref is not None else None
            ),
            'viewpoint': self.viewpoint,
            'sensor_identity': self.sensor_identity,
            'calibration_ref': self.calibration_ref,
            'uncertainty': self.uncertainty,
            'readings': [r.model_dump(mode='json') for r in self.readings],
            'raster_chain': [
                s.model_dump(mode='json') for s in self.raster_chain
            ],
            'crop_attributions': [
                c.model_dump(mode='json') for c in self.crop_attributions
            ],
            'masking_edges': [
                m.model_dump(mode='json') for m in self.masking_edges
            ],
            'physical_alignment': self.physical_alignment,
            'active_corrections': list(self.active_corrections),
            'observed_at_utc': self.observed_at_utc,
            'provenance_json': self.provenance_json,
        }

    def reading_for(
        self, quantity: GeometryQuantity, edge: ImageEdge | None = None
    ) -> CadGeometryQuantityReading | None:
        for reading in self.readings:
            if reading.quantity == quantity and reading.edge == edge:
                return reading
        return None


def measurement_binding(
    measurement: CadImageGeometryMeasurement,
) -> AuthorityRef:
    return AuthorityRef(
        kind='image_geometry_measurement',
        ref_id=measurement.measurement_id,
        ref_sha256=measurement.measurement_sha256,
    )


# ---------------------------------------------------------------------------
# Lens-memory repeatability
# ---------------------------------------------------------------------------


class CadLensMemoryRecallRecord(BaseModel):
    """One lens-memory recall cycle's reproducibility evidence.

    ``requested_state``/``observed_boundary`` stay as declared JSON so
    the exact device vocabulary is preserved; ``position_error`` is the
    measured recall error with units. A preset name is never proof the
    physical image returned to the same boundary.
    """

    model_config = ConfigDict(frozen=True)

    recall_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    firmware: str | None = None
    cycle_index: int = Field(ge=1)
    requested_state_json: str = '{}'
    observed_boundary_json: str = '{}'
    position_error: float | None = None
    position_error_units: str | None = None
    repeatable: bool | None = None
    observed_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    recall_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_recall(self) -> 'CadLensMemoryRecallRecord':
        _require_iso8601(self.observed_at_utc, 'recall observed_at_utc')
        if self.position_error is not None:
            _require_finite(self.position_error, 'recall position_error')
            if self.position_error < 0:
                raise ValueError('position_error must be non-negative')
            if self.position_error_units is None:
                raise ValueError('position_error requires units')
        expected = _hash(self.identity_payload())
        if self.recall_sha256 != expected:
            raise ValueError('lens-memory recall hash mismatch')
        if self.recall_id != _semantic_id('lensrec', expected):
            raise ValueError('recall id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'memory_id': self.memory_id,
            'firmware': self.firmware,
            'cycle_index': self.cycle_index,
            'requested_state_json': self.requested_state_json,
            'observed_boundary_json': self.observed_boundary_json,
            'position_error': self.position_error,
            'position_error_units': self.position_error_units,
            'repeatable': self.repeatable,
            'observed_at_utc': self.observed_at_utc,
            'provenance_json': self.provenance_json,
        }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class CadGeometryEvaluation(BaseModel):
    """Sealed geometry verdict for one presentation profile.

    Every declared quantity state is reported — an absent quantity would
    silently read as fine. ``physical_alignment`` stays separate from
    the verdict so digital warp can never launder a crooked projector.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    binding_ref: AuthorityRef
    measurement_ref: AuthorityRef | None = None
    quantity_states: tuple[tuple[GeometryQuantity, GeometryQuantityState], ...]
    physical_alignment: PhysicalAlignment
    digital_correction_state: DigitalCorrectionState
    correction_costs: tuple[str, ...] = ()
    verdict: GeometryVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'CadGeometryEvaluation':
        _require_iso8601(
            self.evaluated_at_utc, 'geometry evaluation evaluated_at_utc'
        )
        if self.binding_ref.ref_sha256 is None:
            raise ValueError('evaluations must pin the binding sha256')
        if self.measurement_ref is not None and (
            self.measurement_ref.ref_sha256 is None
        ):
            raise ValueError('measurement ref must pin its sha256')
        covered = {quantity for quantity, _ in self.quantity_states}
        if covered != set(ALL_GEOMETRY_QUANTITIES):
            raise ValueError(
                'a geometry evaluation must report every quantity — '
                'an omitted quantity would silently read as PASS'
            )
        if len(self.quantity_states) != len(ALL_GEOMETRY_QUANTITIES):
            raise ValueError('duplicate quantity entries')
        expected = _hash(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('geometry evaluation hash mismatch')
        if self.evaluation_id != _semantic_id('geoeval', expected):
            raise ValueError('evaluation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'binding_ref': self.binding_ref.model_dump(mode='json'),
            'measurement_ref': (
                self.measurement_ref.model_dump(mode='json')
                if self.measurement_ref is not None else None
            ),
            'quantity_states': [
                list(item) for item in self.quantity_states
            ],
            'physical_alignment': self.physical_alignment,
            'digital_correction_state': self.digital_correction_state,
            'correction_costs': list(self.correction_costs),
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def quantity_state(
        self, quantity: GeometryQuantity
    ) -> GeometryQuantityState:
        return dict(self.quantity_states)[quantity]


def geometry_evaluation_binding(
    evaluation: CadGeometryEvaluation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='geometry_evaluation',
        ref_id=evaluation.evaluation_id,
        ref_sha256=evaluation.evaluation_sha256,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_presentation_geometry_binding(
    *,
    document_id: str,
    presentation_profile_ref: AuthorityRef | None = None,
    screen_ref: AuthorityRef | None = None,
    projector_ref: AuthorityRef | None = None,
    signal_profile_ref: AuthorityRef | None = None,
    content_aspect: float | None = None,
    projected_aspect: float | None = None,
    lens_memory_id: str | None = None,
    zoom_ratio: float | None = None,
    lens_shift_h: float | None = None,
    lens_shift_v: float | None = None,
    keystone_state: DigitalCorrectionState = 'unknown',
    warp_state: DigitalCorrectionState = 'unknown',
    anamorphic_state: AnamorphicState = 'unknown',
    processor_scaling_mode: str | None = None,
    masking_state: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadPresentationGeometryBinding:
    """Seal one presentation profile's geometry identity."""
    payload = dict(
        document_id=document_id,
        presentation_profile_ref=presentation_profile_ref,
        screen_ref=screen_ref,
        projector_ref=projector_ref,
        signal_profile_ref=signal_profile_ref,
        content_aspect=content_aspect,
        projected_aspect=projected_aspect,
        lens_memory_id=lens_memory_id,
        zoom_ratio=zoom_ratio,
        lens_shift_h=lens_shift_h,
        lens_shift_v=lens_shift_v,
        keystone_state=keystone_state,
        warp_state=warp_state,
        anamorphic_state=anamorphic_state,
        processor_scaling_mode=processor_scaling_mode,
        masking_state=masking_state,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadPresentationGeometryBinding, payload,
        'binding_id', 'binding_sha256', 'geobind',
    )


def build_image_geometry_measurement(
    *,
    document_id: str,
    binding: CadPresentationGeometryBinding | AuthorityRef,
    method: GeometryMethod,
    test_pattern_identity: str,
    test_pattern_ref: AuthorityRef | None = None,
    viewpoint: str | None = None,
    sensor_identity: str | None = None,
    calibration_ref: str | None = None,
    uncertainty: float | None = None,
    readings: tuple[CadGeometryQuantityReading, ...] = (),
    raster_chain: tuple[CadRasterStageDeclaration, ...] = (),
    crop_attributions: tuple[CadCropAttribution, ...] = (),
    masking_edges: tuple[CadMaskingEdgeState, ...] = (),
    physical_alignment: PhysicalAlignment = 'unknown',
    active_corrections: tuple[CorrectionKind, ...] = (),
    observed_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadImageGeometryMeasurement:
    """Seal one geometry measurement campaign."""
    binding_ref = (
        geometry_binding_ref(binding)
        if isinstance(binding, CadPresentationGeometryBinding)
        else binding
    )
    payload = dict(
        document_id=document_id,
        binding_ref=binding_ref,
        method=method,
        test_pattern_identity=test_pattern_identity,
        test_pattern_ref=test_pattern_ref,
        viewpoint=viewpoint,
        sensor_identity=sensor_identity,
        calibration_ref=calibration_ref,
        uncertainty=uncertainty,
        readings=readings,
        raster_chain=raster_chain,
        crop_attributions=crop_attributions,
        masking_edges=masking_edges,
        physical_alignment=physical_alignment,
        active_corrections=active_corrections,
        observed_at_utc=observed_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadImageGeometryMeasurement, payload,
        'measurement_id', 'measurement_sha256', 'geomeas',
    )


def build_lens_memory_recall(
    *,
    document_id: str,
    memory_id: str,
    cycle_index: int,
    firmware: str | None = None,
    requested_state_json: str = '{}',
    observed_boundary_json: str = '{}',
    position_error: float | None = None,
    position_error_units: str | None = None,
    repeatable: bool | None = None,
    observed_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadLensMemoryRecallRecord:
    """Seal one lens-memory recall cycle."""
    payload = dict(
        document_id=document_id,
        memory_id=memory_id,
        firmware=firmware,
        cycle_index=cycle_index,
        requested_state_json=requested_state_json,
        observed_boundary_json=observed_boundary_json,
        position_error=position_error,
        position_error_units=position_error_units,
        repeatable=repeatable,
        observed_at_utc=observed_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadLensMemoryRecallRecord, payload,
        'recall_id', 'recall_sha256', 'lensrec',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

#: Corrections that consume raster / introduce scaling — their use is
#: legal but must stay explicit with its costs attached.
_RASTER_CONSUMING_CORRECTIONS: frozenset[CorrectionKind] = frozenset(
    {'digital_keystone', 'warp_geometric_processing', 'scaling_crop'}
)


def evaluate_presentation_geometry(
    *,
    document_id: str,
    binding: CadPresentationGeometryBinding,
    measurement: CadImageGeometryMeasurement | None = None,
    tolerances: Mapping[GeometryQuantity, float] | None = None,
    evaluated_at_utc: str | None = None,
) -> CadGeometryEvaluation:
    """Fail-closed geometry verdict for one presentation profile.

    Without a measurement the verdict is ``insufficient_evidence`` —
    nominal throw math is not geometry evidence. A physically
    misaligned projector corrected by digital warp reports
    ``verified_with_digital_correction`` plus its correction costs;
    measured FAIL on aspect/crop/masking quantities fails the profile.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    tolerances = dict(tolerances or {})
    reasons: list[str] = []
    costs: list[str] = []

    states: dict[GeometryQuantity, GeometryQuantityState] = {
        quantity: 'UNKNOWN' for quantity in ALL_GEOMETRY_QUANTITIES
    }
    physical_alignment = (
        measurement.physical_alignment if measurement is not None
        else 'unknown'
    )

    if measurement is not None:
        if measurement.binding_ref.ref_id != binding.binding_id or (
            measurement.binding_ref.ref_sha256 != binding.binding_sha256
        ):
            raise ValueError(
                'measurement does not bind this exact presentation '
                'profile — a mode/profile change is a new qualification'
            )
        # Raster chain honesty: missing stages are explicitly UNKNOWN,
        # never assumed clean.
        stages = {stage.stage for stage in measurement.raster_chain}
        if len(stages) < 5:
            missing = sorted(
                stage for stage in (
                    'source_active_raster', 'processed_output_raster',
                    'projector_imaging_raster', 'projected_image_boundary',
                    'visible_screen_boundary',
                ) if stage not in stages
            )
            reasons.append(
                'raster chain incomplete — unmeasured stages stay '
                f'unknown: {", ".join(missing)}'
            )
        for reading in measurement.readings:
            state = reading.observed_state
            if (
                state is None
                and reading.value is not None
                and reading.quantity in tolerances
            ):
                state = (
                    'PASS'
                    if abs(reading.value) <= tolerances[reading.quantity]
                    else 'FAIL'
                )
            states[reading.quantity] = state or 'UNKNOWN'

        # Masking edges fold into the masking_overlap_gap quantity: a
        # measured intrusion is a FAIL, a measured clean edge a PASS.
        if measurement.masking_edges:
            edge_states = [
                edge.observed_state for edge in measurement.masking_edges
            ]
            if any(s == 'FAIL' for s in edge_states):
                states['masking_overlap_gap'] = 'FAIL'
            elif any(s == 'UNKNOWN' or s is None for s in edge_states):
                if states['masking_overlap_gap'] != 'FAIL':
                    states['masking_overlap_gap'] = 'UNKNOWN'
            elif all(s == 'PASS' for s in edge_states):
                states['masking_overlap_gap'] = 'PASS'

        for correction in measurement.active_corrections:
            if correction in _RASTER_CONSUMING_CORRECTIONS:
                costs.append(
                    f'{correction} consumes image raster / resolution '
                    'and adds scaling/latency — explicit, not free'
                )
        if measurement.physical_alignment == 'physically_misaligned' and (
            measurement.active_corrections
        ):
            reasons.append(
                'digital correction compensates a physically misaligned '
                'projector — the pose fault stays on the record'
            )
        if measurement.physical_alignment == 'physically_misaligned' and (
            not measurement.active_corrections
        ):
            reasons.append(
                'projector physically misaligned with no correction '
                'declared'
            )

    # Anamorphic consistency — a binding-level integrity check.
    if binding.anamorphic_state == 'electronic_stretch':
        reasons.append(
            'electronic stretch declared — requires the anamorphic lens '
            'to be documented in the optical path; verify lens state'
        )
    anamorphic_conflict = (
        (binding.anamorphic_state == 'electronic_stretch')
        and measurement is not None
        and 'optical_anamorphic' not in measurement.active_corrections
        and binding.projected_aspect is not None
        and binding.content_aspect is not None
        and abs(binding.projected_aspect - binding.content_aspect) < 1e-6
    )

    digital_correction = binding.digital_correction_active() or (
        measurement is not None
        and bool(
            _RASTER_CONSUMING_CORRECTIONS
            & frozenset(measurement.active_corrections)
        )
    )
    digital_state: DigitalCorrectionState = (
        'active' if digital_correction
        else ('none' if measurement is not None or (
            binding.keystone_state == 'none' and binding.warp_state == 'none'
        ) else 'unknown')
    )

    if measurement is None:
        verdict: GeometryVerdict = 'insufficient_evidence'
        reasons.append(
            'no geometry measurement bound — nominal throw calculation '
            'is not image-geometry evidence'
        )
    elif anamorphic_conflict:
        verdict = 'failed'
        reasons.append(
            'anamorphic stretch state inconsistent with the optical '
            'path — geometry fails explicitly'
        )
    elif 'FAIL' in states.values():
        verdict = 'failed'
    elif digital_correction:
        verdict = 'verified_with_digital_correction'
    elif 'UNKNOWN' in states.values():
        verdict = 'verified_with_limitations'
        reasons.append(
            'some geometry quantities unmeasured — verdict is limited '
            'to the quantities that were actually observed'
        )
    else:
        verdict = 'verified'

    payload = dict(
        document_id=document_id,
        binding_ref=geometry_binding_ref(binding),
        measurement_ref=(
            measurement_binding(measurement)
            if measurement is not None else None
        ),
        quantity_states=tuple(
            (quantity, states[quantity])
            for quantity in ALL_GEOMETRY_QUANTITIES
        ),
        physical_alignment=physical_alignment,
        digital_correction_state=digital_state,
        correction_costs=tuple(costs),
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=GEOMETRY_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadGeometryEvaluation, payload,
        'evaluation_id', 'evaluation_sha256', 'geoeval',
    )


__all__ = [
    'GEOMETRY_AUTHORITY_SCHEMA_VERSION',
    'GEOMETRY_EVALUATION_VERSION',
    'ALL_GEOMETRY_QUANTITIES',
    'AnamorphicState',
    'CadCropAttribution',
    'CadGeometryEvaluation',
    'CadGeometryQuantityReading',
    'CadImageGeometryMeasurement',
    'CadLensMemoryRecallRecord',
    'CadMaskingEdgeState',
    'CadPresentationGeometryBinding',
    'CadRasterStageDeclaration',
    'CorrectionKind',
    'CropSource',
    'DigitalCorrectionState',
    'GeometryMethod',
    'GeometryQuantity',
    'GeometryQuantityState',
    'GeometryVerdict',
    'ImageEdge',
    'PhysicalAlignment',
    'RasterStage',
    'build_image_geometry_measurement',
    'build_lens_memory_recall',
    'build_presentation_geometry_binding',
    'evaluate_presentation_geometry',
    'geometry_binding_ref',
    'geometry_evaluation_binding',
    'measurement_binding',
]
