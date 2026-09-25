"""Comparison semantic integrity (#852): distinguish absolute-level,
normalized-shape and diagnostic-only A/B compatibility.

``compare_frequency_responses`` only ever sees numeric frequency/level
arrays — computability never implies comparability. This module snapshots
each side's semantic context from the canonical authorities at comparison
time (level-reference kind + bound calibration, acquisition context,
routing profile, timing group, effective corrected binding, quality
state), derives typed metric eligibility, and produces the advisory
mismatch list that history persists verbatim — a saved comparison later
reopens with the *same* semantic decisions regardless of how the project
has since evolved.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .cad_measurement_effective import (
    CadEffectiveMeasurementResolver,
    EffectiveMeasurementEvidence,
)


LevelCompatibility = Literal[
    'absolute_level_comparable',
    'normalized_shape_comparable',
    'diagnostic_only',
    'incompatible',
]
MetricAvailability = Literal['available', 'unavailable']

#: Advisory mismatch axes; intentional seat/channel/before-after differences
#: stay allowed — they are labelled, never blocked (#483/#852).
COMPARISON_MISMATCH_AXES = (
    'evidence_type',
    'channel_role',
    'target',
    'source_speakers',
    'scene_revision',
    'smoothing',
    'acquisition_context',
    'routing_profile',
    'level_reference',
    'timing_reference',
    'radiation_scope',
)


class ComparisonSideContext(BaseModel):
    """Immutable semantic snapshot of one comparison side.

    Exact refs/hashes only — never a copy of authority payloads.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    dataset_id: str
    dataset_sha256: str
    evidence_type: str
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    # Effective (correction-overlaid) semantic binding.
    measurement_entity_id: str
    channel_role: str
    source_speaker_ids: tuple[str, ...]
    radiation_scope: str
    routing_evidence: str
    acquisition_context_id: str | None = None
    acquisition_context_sha256: str | None = None
    routing_profile_id: str | None = None
    routing_profile_sha256: str | None = None
    level_reference_kind: str = 'unknown'
    calibration_id: str | None = None
    calibration_sha256: str | None = None
    #: Common-timing identity token — two sides compare phase/arrival only
    #: inside one timing group.
    timing_group: str | None = None
    quality_report_state: str = 'missing'
    retake_recommendation: str | None = None
    smoothing: str | None = None
    captured_at: str | None = None
    #: Human A/B identity, e.g. "MLP · front_left · measured".
    label: str = ''


class ComparisonSemantics(BaseModel):
    """Typed compatibility + advisory context for one A/B pair."""

    model_config = ConfigDict(frozen=True)

    side_a: ComparisonSideContext
    side_b: ComparisonSideContext
    level_compatibility: LevelCompatibility
    mismatches: tuple[str, ...] = ()
    absolute_level: MetricAvailability = 'unavailable'
    absolute_level_reason: str = ''
    normalized_shape: MetricAvailability = 'unavailable'
    normalized_shape_reason: str = ''
    common_time_phase: MetricAvailability = 'unavailable'
    common_time_phase_reason: str = ''
    reference_band_hz: tuple[float, float] | None = None
    label_a: str = ''
    label_b: str = ''


def _timing_group(context: Any) -> str | None:
    """Identity token for the acquisition context's timing reference."""
    if context is None:
        return None
    timing_id = getattr(context, 'timing_reference_id', None)
    if not timing_id:
        return None
    return '|'.join(
        str(part)
        for part in (
            timing_id,
            getattr(context, 'clock_source', None) or '',
            getattr(context, 'sample_rate_hz', None) or '',
        )
    )


def resolve_comparison_side(
    *,
    measurement_repository,
    quality_repository,
    measurement_id: str,
    target_name: str | None = None,
) -> tuple[EffectiveMeasurementEvidence, ComparisonSideContext]:
    """Snapshot one side from canonical authority (#852).

    Runs through the shared effective-measurement resolver: dispositioned
    evidence raises ``ValueError``, and the returned context carries the
    corrected binding — never the stale original assignment.
    """
    evidence = CadEffectiveMeasurementResolver(
        measurement_repository, quality_repository
    ).require_normal_use(measurement_id, purpose='measurement comparison')
    measurement = evidence.measurement
    dataset = getattr(measurement_repository, 'dataset_for_measurement')(
        measurement_id
    )
    report = quality_repository.latest_report(measurement_id)
    report_state = 'missing'
    if report is not None:
        from .cad_measurement_quality import (
            dataset_sha256 as _dataset_sha256,
            measurement_sha256,
        )

        report_state = (
            'current'
            if dataset is not None
            and report.measurement_sha256 == measurement_sha256(measurement)
            and report.dataset_sha256 == _dataset_sha256(dataset)
            else 'stale'
        )
    context_binding = None
    context = None
    if report is not None:
        context_binding = report.acquisition_context
    if context_binding is not None:
        context = quality_repository.get_acquisition_context(
            context_binding.acquisition_context_id
        )
    try:
        provenance = json.loads(getattr(measurement, 'provenance_json', '') or '{}')
    except (TypeError, ValueError):
        provenance = {}
    if not isinstance(provenance, dict):
        provenance = {}
    level_reference = (
        None
        if dataset is None
        else quality_repository.get_dataset_level_reference(dataset.dataset_id)
    )
    smoothing = None if dataset is None else getattr(dataset, 'smoothing', None)
    target_label = target_name or evidence.measurement_entity_id
    label = (
        f'{target_label} · {evidence.channel_role} · {measurement.evidence_type}'
    )
    return evidence, ComparisonSideContext(
        measurement_id=measurement.measurement_id,
        dataset_id='' if dataset is None else dataset.dataset_id,
        dataset_sha256='' if dataset is None else dataset.dataset_sha256,
        evidence_type=measurement.evidence_type,
        document_id=measurement.document_id,
        scene_revision_id=measurement.scene_revision_id,
        scene_content_hash=measurement.scene_content_hash,
        measurement_entity_id=evidence.measurement_entity_id,
        channel_role=evidence.channel_role,
        source_speaker_ids=tuple(evidence.source_speaker_ids),
        radiation_scope=evidence.radiation_scope,
        routing_evidence=evidence.routing_evidence,
        acquisition_context_id=(
            None if context_binding is None else context_binding.acquisition_context_id
        ),
        acquisition_context_sha256=(
            None
            if context_binding is None
            else context_binding.acquisition_context_sha256
        ),
        routing_profile_id=provenance.get('routing_profile_id'),
        routing_profile_sha256=provenance.get('routing_profile_sha256'),
        level_reference_kind=(
            'unknown' if level_reference is None
            else level_reference.level_reference_kind
        ),
        calibration_id=(
            None if level_reference is None else level_reference.calibration_id
        ),
        calibration_sha256=(
            None
            if level_reference is None
            else level_reference.calibration_sha256
        ),
        timing_group=_timing_group(context),
        quality_report_state=report_state,
        retake_recommendation=(
            None if report is None else report.retake_recommendation
        ),
        smoothing=smoothing,
        captured_at=getattr(measurement, 'captured_at', None),
        label=label,
    )


def derive_comparison_semantics(
    *,
    side_a: ComparisonSideContext,
    side_b: ComparisonSideContext,
    reference_band_hz: tuple[float, float] | None = None,
) -> ComparisonSemantics:
    """Derive advisory mismatches + typed metric eligibility for a pair.

    Absolute-level metrics require both sides bound to applicable absolute
    SPL calibrations; a requested reference band can still permit an
    explicitly *normalized shape* comparison without ever upgrading the
    absolute-level claim.
    """
    mismatches: list[str] = []
    if side_a.evidence_type != side_b.evidence_type:
        mismatches.append('evidence_type')
    if side_a.channel_role != side_b.channel_role:
        mismatches.append('channel_role')
    if side_a.measurement_entity_id != side_b.measurement_entity_id:
        mismatches.append('target')
    if side_a.source_speaker_ids != side_b.source_speaker_ids:
        mismatches.append('source_speakers')
    if (
        side_a.scene_revision_id != side_b.scene_revision_id
        or side_a.scene_content_hash != side_b.scene_content_hash
    ):
        mismatches.append('scene_revision')
    if side_a.smoothing != side_b.smoothing:
        mismatches.append('smoothing')
    if side_a.acquisition_context_sha256 != side_b.acquisition_context_sha256:
        mismatches.append('acquisition_context')
    if side_a.routing_profile_sha256 != side_b.routing_profile_sha256:
        mismatches.append('routing_profile')
    if (
        side_a.level_reference_kind != side_b.level_reference_kind
        or side_a.calibration_sha256 != side_b.calibration_sha256
    ):
        mismatches.append('level_reference')
    if side_a.timing_group != side_b.timing_group:
        mismatches.append('timing_reference')
    if side_a.radiation_scope != side_b.radiation_scope:
        mismatches.append('radiation_scope')

    absolute_ok = (
        side_a.level_reference_kind == 'absolute_spl'
        and side_b.level_reference_kind == 'absolute_spl'
        and side_a.calibration_sha256 is not None
        and side_b.calibration_sha256 is not None
    )
    same_semantics = side_a.level_reference_kind == side_b.level_reference_kind
    normalized_ok = same_semantics or reference_band_hz is not None

    if absolute_ok:
        level_compatibility: LevelCompatibility = 'absolute_level_comparable'
        absolute: MetricAvailability = 'available'
        absolute_reason = ''
        shape: MetricAvailability = 'available'
        shape_reason = ''
    elif normalized_ok:
        level_compatibility = 'normalized_shape_comparable'
        absolute = 'unavailable'
        absolute_reason = (
            'level references differ or are not absolute SPL authorities; '
            'raw level difference is not an absolute acoustic difference'
        )
        shape = 'available'
        shape_reason = (
            'explicit reference-band normalization'
            if reference_band_hz is not None and not same_semantics
            else 'matching level semantics'
        )
    else:
        level_compatibility = 'diagnostic_only'
        absolute = 'unavailable'
        absolute_reason = (
            'incompatible or unknown level semantics; no absolute difference'
        )
        shape = 'unavailable'
        shape_reason = (
            'different level semantics and no reference-band normalization'
        )

    if (
        side_a.timing_group is not None
        and side_a.timing_group == side_b.timing_group
    ):
        common_time: MetricAvailability = 'available'
        common_time_reason = ''
    else:
        common_time = 'unavailable'
        common_time_reason = (
            'sides do not share one timing reference group; phase/arrival '
            'differences are not common-time comparable'
        )

    return ComparisonSemantics(
        side_a=side_a,
        side_b=side_b,
        level_compatibility=level_compatibility,
        mismatches=tuple(mismatches),
        absolute_level=absolute,
        absolute_level_reason=absolute_reason,
        normalized_shape=shape,
        normalized_shape_reason=shape_reason,
        common_time_phase=common_time,
        common_time_phase_reason=common_time_reason,
        reference_band_hz=reference_band_hz,
        label_a=side_a.label,
        label_b=side_b.label,
    )
