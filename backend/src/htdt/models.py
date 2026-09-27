from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .geometry import polygon_covers_xy, validate_polygon_in_reference_box
from .limits import MAX_ATTACHMENT_BASE64_CHARS, MAX_REW_TEXT_BASE64_CHARS


EvidenceType = Literal['measured', 'derived', 'predicted', 'unknown']
RadiationScope = Literal['single', 'bass_managed', 'mixed', 'unknown']
QualityStatus = Literal['usable', 'warning', 'invalid', 'unknown']
QualitySource = Literal['manual', 'imported', 'derived', 'unknown']
RoutingEvidence = Literal['verified', 'manual', 'inferred', 'unknown']
AttachmentKind = Literal['mdat', 'microphone_calibration', 'avr_settings', 'measurement_note', 'image', 'other']


class RoomVertex(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    vertex_id: str = Field(min_length=1, max_length=100)
    x_m: float
    y_m: float


class Point3D(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    x_m: float
    y_m: float
    z_m: float


class SpeakerPlacement(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    speaker_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    model: str | None = None
    position: Point3D | None = None
    aim_xyz: tuple[float, float, float] | None = None
    mounting_type: str | None = None


class RoomSnapshot(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    width_m: float = Field(gt=0)
    depth_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    geometry_kind: Literal['rectangular', 'reference_box', 'polygon_prism'] = 'rectangular'
    footprint_vertices: list[RoomVertex] | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def validate_geometry(self) -> 'RoomSnapshot':
        if self.geometry_kind == 'polygon_prism':
            if self.footprint_vertices is None:
                raise ValueError('polygon_prism requires footprint_vertices')
            vertex_ids = [vertex.vertex_id for vertex in self.footprint_vertices]
            if len(set(vertex_ids)) != len(vertex_ids):
                raise ValueError('Room polygon vertex_id values must be unique')
            validate_polygon_in_reference_box(
                self.width_m, self.depth_m,
                [(point.x_m, point.y_m) for point in self.footprint_vertices],
            )
        elif self.footprint_vertices is not None:
            raise ValueError('footprint_vertices is only valid for polygon_prism geometry')
        return self


class MeasurementPoint(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    point_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    position: Point3D
    aim_xyz: tuple[float, float, float] | None = None
    position_precision_m: float | None = Field(default=None, ge=0)


class MicrophoneSnapshot(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    manufacturer: str = 'miniDSP'
    model: str = 'UMIK-1'
    serial: str | None = None
    connection: Literal['usb', 'other', 'unknown'] = 'usb'
    sample_rate_hz: int | None = Field(default=48000, gt=0)
    calibration_profile: Literal['0deg', '90deg', 'unknown'] = '90deg'
    calibration_filename: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def validate_known_sample_rate(self) -> 'MicrophoneSnapshot':
        normalized = self.model.upper().replace(' ', '')
        if normalized.startswith('UMIK-1') and self.sample_rate_hz not in (None, 48000):
            raise ValueError('UMIK-1 must use 48000 Hz')
        return self


class AVRConfiguration(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    manufacturer: str = 'Yamaha'
    model: str = 'RX-A4A'
    firmware: str | None = None
    input_name: str | None = None
    volume_db: float | None = None
    processing_mode: str | None = None
    peq_mode: str | None = None
    extra: dict[str, str | float | int | bool | None] = Field(default_factory=dict)


class ContextCreate(BaseModel):
    room: RoomSnapshot
    speakers: list[SpeakerPlacement]
    measurement_point: MeasurementPoint
    microphone: MicrophoneSnapshot | None = None
    avr: AVRConfiguration = Field(default_factory=AVRConfiguration)
    notes: str | None = None
    parent_context_id: str | None = None

    @model_validator(mode='after')
    def validate_positions(self) -> 'ContextCreate':
        room = self.room
        speaker_ids = [speaker.speaker_id for speaker in self.speakers]
        if len(speaker_ids) != len(set(speaker_ids)):
            raise ValueError('speaker_id values must be unique within a Context')
        if self.measurement_point.point_id in set(speaker_ids):
            raise ValueError('measurement point_id must not duplicate a speaker_id')
        positions: list[tuple[str, Point3D]] = [('measurement point', self.measurement_point.position)]
        positions.extend((speaker.speaker_id, speaker.position) for speaker in self.speakers if speaker.position is not None)
        polygon = None
        if room.geometry_kind == 'polygon_prism' and room.footprint_vertices is not None:
            polygon = validate_polygon_in_reference_box(
                room.width_m, room.depth_m,
                [(point.x_m, point.y_m) for point in room.footprint_vertices],
            )
        for label, point in positions:
            if not (0 <= point.x_m <= room.width_m and 0 <= point.y_m <= room.depth_m and 0 <= point.z_m <= room.height_m):
                raise ValueError(f'{label} is outside the room reference bounds')
            if polygon is not None and not polygon_covers_xy(polygon, point.x_m, point.y_m):
                raise ValueError(f'{label} is outside the room polygon')
        return self


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class SessionCreate(BaseModel):
    purpose: str | None = Field(default=None, max_length=300)
    started_at: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


class ImportPreviewRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    raw_base64: str = Field(min_length=1, max_length=MAX_REW_TEXT_BASE64_CHARS)


class MeasurementImportRequest(ImportPreviewRequest):
    context_id: str
    session_id: str | None = None
    channel_role: str = Field(min_length=1)
    evidence_type: EvidenceType = 'unknown'
    source_speaker_ids: list[str] = Field(default_factory=list)
    radiation_scope: RadiationScope = 'unknown'
    routing_evidence: RoutingEvidence = 'unknown'
    captured_at: str | None = None
    notes: str | None = None
    quality_status: QualityStatus = 'unknown'
    quality_reasons: list[str] = Field(default_factory=list)
    quality_source: QualitySource = 'unknown'
    repeat_group: str | None = Field(default=None, max_length=200)


class RewApiSnapshotImportRequest(BaseModel):
    measurement_uuid: str = Field(min_length=1, max_length=200)
    context_id: str
    session_id: str | None = None
    channel_role: str = Field(min_length=1)
    evidence_type: EvidenceType = 'unknown'
    source_speaker_ids: list[str] = Field(default_factory=list)
    radiation_scope: RadiationScope = 'unknown'
    routing_evidence: RoutingEvidence = 'unknown'
    notes: str | None = None
    quality_status: QualityStatus = 'unknown'
    quality_reasons: list[str] = Field(default_factory=list)
    quality_source: QualitySource = 'unknown'
    repeat_group: str | None = Field(default=None, max_length=200)
    unit: str = Field(default='SPL', min_length=1, max_length=32)
    ppo: int | None = Field(default=None, ge=1, le=384)
    smoothing: str | None = Field(default=None, max_length=32)


class AttachmentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    raw_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BASE64_CHARS)
    kind: AttachmentKind = 'other'
    label: str | None = Field(default=None, max_length=300)
    measurement_id: str | None = None
    context_id: str | None = None


class ExcludedBand(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    low_hz: float = Field(gt=0)
    high_hz: float = Field(gt=0)

    @model_validator(mode='after')
    def validate_order(self) -> 'ExcludedBand':
        if self.high_hz <= self.low_hz:
            raise ValueError('high_hz must be greater than low_hz')
        return self


class ComparisonCreate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    dataset_a_id: str
    dataset_b_id: str
    low_hz: float = Field(gt=0)
    high_hz: float = Field(gt=0)
    reference_low_hz: float | None = Field(default=None, gt=0)
    reference_high_hz: float | None = Field(default=None, gt=0)
    excluded_bands: list[ExcludedBand] = Field(default_factory=list)
    expected_change_paths: list[str] = Field(default_factory=list)
    label: str | None = None

    @model_validator(mode='after')
    def validate_bands(self) -> 'ComparisonCreate':
        if self.high_hz <= self.low_hz:
            raise ValueError('high_hz must be greater than low_hz')
        if (self.reference_low_hz is None) != (self.reference_high_hz is None):
            raise ValueError('reference band requires both bounds')
        if self.reference_low_hz is not None and self.reference_high_hz is not None:
            if self.reference_high_hz <= self.reference_low_hz:
                raise ValueError('reference_high_hz must be greater than reference_low_hz')
        return self
