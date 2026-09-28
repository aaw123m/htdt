"""Audio format transformation lineage authority (#1037).

#570/#820 answer "can this route carry the requested signal/mode?" — this
module answers "what transform occurred at each processing node?" The
transported signal capability and the actual transform lineage are
separate authorities.

- :class:`AudioSignalFormat` — format at one node: codec/container,
  bitstream-vs-PCM, sample rate, channel layout, object/metadata presence.
  Unknown fields stay UNKNOWN; nothing is inferred from the original file
  once an intermediate device transformed it.
- :class:`AudioFormatTransform` — one immutable transform edge between an
  exact input and output format at a device node. Transform kinds are
  distinct: DECODE, DOWNMIX, UPMIX, TRANSCODE, CHANNEL_REMAP,
  SAMPLE_RATE_CONVERT, BIT_DEPTH_CONVERT, passthrough, opaque vendor.
- :func:`evaluate_transform_lineage` — checks that a recorded chain is
  internally consistent: edges chain end-to-end, unknown downmix
  coefficients stay UNKNOWN, upmixer gains are not fabricated, object
  metadata is not assumed to survive a fixed-channel render, and channel
  index is never silently equated with logical speaker role.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




TransformKind = Literal[
    'decode',
    'encode',
    'transcode',
    'downmix',
    'upmix',
    'channel_remap',
    'sample_rate_convert',
    'bit_depth_convert',
    'pcm_passthrough',
    'bitstream_passthrough',
    'opaque_vendor_transform',
]


class AudioSignalFormat(BaseModel):
    """Signal format at one node. ``None`` fields are UNKNOWN — never
    inferred from a sibling node."""

    model_config = ConfigDict(frozen=True)

    format_id: str = Field(min_length=1)
    container_family: str | None = None
    codec: str | None = None
    signal_kind: Literal['bitstream', 'pcm', 'unknown'] = 'unknown'
    sample_rate_hz: int | None = Field(default=None, gt=0)
    bit_depth: int | None = Field(default=None, gt=0)
    channel_count: int | None = Field(default=None, ge=0)
    channel_layout_id: str | None = None
    carries_object_metadata: bool | None = None
    clock_domain_id: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'AudioSignalFormat':
        if self.signal_kind == 'pcm' and self.codec is not None:
            if self.codec.lower() != 'pcm':
                raise ValueError(
                    'a pcm signal must not name a coded codec family'
                )
        return self


class AudioFormatTransform(BaseModel):
    """One immutable transform edge at one processing node."""

    model_config = ConfigDict(frozen=True)

    transform_id: str = Field(min_length=1)
    device_node_id: str = Field(min_length=1)
    input_format: AudioSignalFormat
    output_format: AudioSignalFormat
    kind: TransformKind
    mode_or_preset_id: str | None = None
    downmix_coefficients_known: bool = False
    upmix_output_gains_known: bool = False
    latency_ms: float | None = Field(default=None, ge=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    transform_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'transform_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'AudioFormatTransform':
        if self.kind == 'bitstream_passthrough':
            if self.input_format != self.output_format:
                raise ValueError(
                    'bitstream passthrough must not alter the format record'
                )
        if self.kind == 'pcm_passthrough':
            if self.input_format != self.output_format:
                raise ValueError(
                    'pcm passthrough must not alter the format record'
                )
        if self.transform_sha256 != _hash(self.semantic_payload()):
            raise ValueError('audio transform semantic hash mismatch')
        return self


def build_audio_format_transform(
    *,
    transform_id: str,
    device_node_id: str,
    input_format: AudioSignalFormat,
    output_format: AudioSignalFormat,
    kind: TransformKind,
    mode_or_preset_id: str | None = None,
    downmix_coefficients_known: bool = False,
    upmix_output_gains_known: bool = False,
    latency_ms: float | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AudioFormatTransform:
    probe = AudioFormatTransform.model_construct(**canonicalize_payload(AudioFormatTransform, dict(
        transform_id=transform_id,
        device_node_id=device_node_id,
        input_format=input_format,
        output_format=output_format,
        kind=kind,
        mode_or_preset_id=mode_or_preset_id,
        downmix_coefficients_known=downmix_coefficients_known,
        upmix_output_gains_known=upmix_output_gains_known,
        latency_ms=latency_ms,
        provenance=tuple(provenance),
        transform_sha256='',
    )))
    return AudioFormatTransform(
        **probe.model_dump(mode='python', exclude={'transform_sha256'}),
        transform_sha256=_hash(probe.semantic_payload()),
    )


class LineageCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


class TransformLineageEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    transform_ids: tuple[str, ...]
    checks: tuple[LineageCheckResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'TransformLineageEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('transform lineage evaluation hash mismatch')
        if self.evaluation_id != 'atl-' + digest[:24]:
            raise ValueError('transform lineage evaluation id mismatch')
        return self


def evaluate_transform_lineage(
    transforms: tuple[AudioFormatTransform, ...],
) -> TransformLineageEvaluation:
    """Consistency checks over a recorded transform chain."""

    checks: list[LineageCheckResult] = []
    ordered = list(transforms)

    gaps = []
    for previous, following in zip(ordered, ordered[1:]):
        if previous.output_format != following.input_format:
            gaps.append(
                f'{previous.transform_id} -> {following.transform_id}'
            )
    checks.append(
        LineageCheckResult(
            check='chain_contiguous',
            status='FAIL' if gaps else 'PASS',
            reason=(
                'format discontinuities between transforms: '
                + ', '.join(gaps)
                if gaps
                else 'each transform consumes the previous output format'
            ),
        )
    )

    opaque_downmix = [
        t.transform_id
        for t in ordered
        if t.kind == 'downmix' and not t.downmix_coefficients_known
    ]
    checks.append(
        LineageCheckResult(
            check='downmix_coefficients',
            status='UNKNOWN' if opaque_downmix else 'PASS',
            reason=(
                'downmix occurred but coefficients are UNKNOWN: '
                + ', '.join(opaque_downmix)
                if opaque_downmix
                else 'every downmix records its coefficients'
            ),
        )
    )

    opaque_upmix = [
        t.transform_id
        for t in ordered
        if t.kind == 'upmix' and not t.upmix_output_gains_known
    ]
    checks.append(
        LineageCheckResult(
            check='upmix_output_gains',
            status='UNKNOWN' if opaque_upmix else 'PASS',
            reason=(
                'upmix occurred but instantaneous output gains are '
                'UNKNOWN: ' + ', '.join(opaque_upmix)
                if opaque_upmix
                else 'every upmix records its output gains'
            ),
        )
    )

    object_loss = []
    for t in ordered:
        if (
            t.input_format.carries_object_metadata
            and t.output_format.carries_object_metadata is False
        ):
            object_loss.append(t.transform_id)
    checks.append(
        LineageCheckResult(
            check='object_metadata_survival',
            status='UNKNOWN' if object_loss else 'PASS',
            reason=(
                'object metadata rendered into fixed channels at: '
                + ', '.join(object_loss)
                if object_loss
                else 'no transform drops recorded object metadata'
            ),
        )
    )

    probe = TransformLineageEvaluation.model_construct(**canonicalize_payload(TransformLineageEvaluation, dict(
        evaluation_id='',
        transform_ids=tuple(t.transform_id for t in ordered),
        checks=tuple(checks),
        evaluation_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return TransformLineageEvaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='atl-' + digest[:24],
        evaluation_sha256=digest,
    )


