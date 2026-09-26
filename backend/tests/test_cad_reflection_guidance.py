"""#876: proven path authority becomes ordered, actionable guidance —
treatment zones, source repositioning, measured-verification gates — and
the interactive scrub session computes exact preview deltas per frame.
"""

from __future__ import annotations

from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from htdt.cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    DeterministicAcousticPath,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_reflection_diagnostic import (
    ReflectionMeasuredTimingEvidence,
    build_interference_hypothesis,
    build_reflection_diagnostic_request,
    match_etc_peaks,
)
from htdt.cad_reflection_guidance import (
    ReflectionGuidanceReport,
    build_reflection_guidance,
    open_guidance_session,
    scrub_source,
)
from htdt.cad_scene import Direction3, Position3
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _authority(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test:{label}',
        authority_version='1',
        semantic_hash_sha256=_hash(label),
    )


SOLVER_REF = _authority('solver')


def _band(surface_id: str | None = None) -> DeterministicPathBandQuantity:
    return DeterministicPathBandQuantity(
        center_hz=100.0,
        spreading_factor_per_m2=1.0,
        source_directivity=SourceDirectivityContribution(
            dataset_id='dataset-1',
            dataset_version='1',
            dataset_semantic_sha256=_hash('dataset'),
            evaluation_semantic_sha256=_hash('eval'),
            frequency_hz=100.0,
            horizontal_angle_deg=0.0,
            vertical_angle_deg=0.0,
            magnitude_db=0.0,
            magnitude_linear=1.0,
            energy_factor=1.0,
        ),
        boundary_material=(
            None
            if surface_id is None
            else BoundaryMaterialContribution(
                source_surface_id=surface_id,
                material_authority=_authority('material'),
                frequency_hz=100.0,
                absorption=0.1,
                scattering=0.0,
                specular_energy_factor=0.9,
            )
        ),
        relative_energy_transport_per_m2=1.0,
    )


def _path(
    *,
    path_type: str,
    length_m: float,
    delay_s: float,
    surface_id: str | None = None,
    point: Position3 | None = None,
) -> DeterministicAcousticPath:
    kwargs = dict(
        source_entity_id='source-1',
        receiver_id='seat-1',
        receiver_entity_id='entity-seat',
        path_type=path_type,
        ordered_interaction_surface_ids=(
            () if surface_id is None else (surface_id,)
        ),
        ordered_interaction_points=(
            () if point is None else (point,)
        ),
        geometric_path_length_m=length_m,
        propagation_delay_s=delay_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=-1.0, y=0.0, z=0.0),
        bands=(_band(surface_id),),
        solver_implementation_ref=SOLVER_REF,
    )
    probe = DeterministicAcousticPath.model_construct(
        path_id='deterministic-acoustic-path:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


def _request(direct: DeterministicAcousticPath, reflection: DeterministicAcousticPath):
    return build_reflection_diagnostic_request(
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        source_entity_id='source-1',
        receiver_id='seat-1',
        receiver_entity_id='entity-seat',
        direct_path_id=direct.path_id,
        reflection_path_id=reflection.path_id,
        speed_of_sound_m_s=343.0,
        solver_implementation_ref=SOLVER_REF,
    )


def _pair(
    direct_len: float = 5.0,
    reflection_len: float = 7.0,
):
    direct = _path(
        path_type='direct',
        length_m=direct_len,
        delay_s=direct_len / 343.0,
    )
    reflection = _path(
        path_type='specular_reflection',
        length_m=reflection_len,
        delay_s=reflection_len / 343.0,
        surface_id='wall-left',
        point=Position3(x_m=0.0, y_m=2.0, z_m=1.0),
    )
    return direct, reflection, _request(direct, reflection)


def _timing_evidence(**overrides) -> ReflectionMeasuredTimingEvidence:
    kwargs = dict(
        measurement_id='measurement:etc-1',
        measurement_artifact_sha256=_hash('etc-peaks'),
        ir_artifact_id='ir:measured-1',
        ir_artifact_sha256=_hash('ir'),
        acquisition_context_id='acq-ctx-1',
        acquisition_context_sha256=_hash('acq'),
        measurement_quality_report_id='mq-report-1',
        measurement_quality_report_sha256=_hash('mq'),
        timing_reference='loopback_calibrated',
        common_source_timebase=True,
        etc_zero_semantics='absolute_time_zero',
        applied_delay_correction_s=0.001,
    )
    kwargs.update(overrides)
    return ReflectionMeasuredTimingEvidence(**kwargs)


def test_guidance_items_bound_to_path_authority() -> None:
    direct, reflection, request = _pair()
    report = build_reflection_guidance(
        request, direct, (reflection,)
    )
    assert report.report_id.startswith('reflection-guidance-report:')
    assert report.request_id == request.request_id
    assert report.request_semantic_sha256 == request.semantic_sha256
    assert report.status == 'actionable'

    kinds = [item.kind for item in report.items]
    assert kinds == ['treat_reflection_zone', 'reposition_source']
    treat, reposition = report.items
    assert treat.surface_id == 'wall-left'
    assert treat.reflection_point is not None
    assert treat.confidence == 'unverified_hypothesis'
    assert treat.excess_delay_s == pytest.approx(2.0 / 343.0)
    assert treat.path_id == reflection.path_id
    assert reposition.source_entity_id == 'source-1'
    # Ranks are a strict total order and ids are content-addressed.
    assert treat.rank == 1 and reposition.rank == 2
    assert treat.item_id != reposition.item_id


def test_guidance_rejects_unbound_or_foreign_paths() -> None:
    direct, reflection, request = _pair()
    other_direct = _path(
        path_type='direct', length_m=4.0, delay_s=4.0 / 343.0
    )
    with pytest.raises(ValueError, match='direct path'):
        build_reflection_guidance(request, other_direct, (reflection,))

    not_specular = _path(
        path_type='direct', length_m=7.0, delay_s=7.0 / 343.0
    )
    with pytest.raises(ValueError, match='specular'):
        build_reflection_guidance(request, direct, (not_specular,))


def test_measured_supported_confidence() -> None:
    direct, reflection, request = _pair()
    etc = match_etc_peaks(
        request,
        (direct, reflection),
        (5.0 / 343.0, 7.0 / 343.0),
        measured_timing=_timing_evidence(),
    )
    hypothesis = build_interference_hypothesis(request, direct, reflection)
    report = build_reflection_guidance(
        request,
        direct,
        (reflection,),
        hypotheses=(hypothesis,),
        etc_report=etc,
    )
    treat = report.items[0]
    assert treat.confidence == 'measured_supported'
    assert treat.etc_report_id == etc.report_id
    assert treat.hypothesis_id == hypothesis.hypothesis_id
    # Measured support present — no verify gate needed.
    assert 'verify_with_measurement' not in {
        item.kind for item in report.items
    }


def test_unverified_hypothesis_gates_treatment() -> None:
    direct, reflection, request = _pair()
    hypothesis = build_interference_hypothesis(request, direct, reflection)
    report = build_reflection_guidance(
        request, direct, (reflection,), hypotheses=(hypothesis,)
    )
    kinds = [item.kind for item in report.items]
    assert kinds == [
        'treat_reflection_zone',
        'reposition_source',
        'verify_with_measurement',
    ]
    verify = report.items[2]
    assert verify.confidence == 'unverified_hypothesis'
    assert verify.hypothesis_id == hypothesis.hypothesis_id


def test_unsupported_measured_peak_narrows_report() -> None:
    direct, reflection, request = _pair()
    etc = match_etc_peaks(
        request,
        (direct, reflection),
        (5.0 / 343.0, 0.1),
        measured_timing=_timing_evidence(),
    )
    report = build_reflection_guidance(
        request, direct, (reflection,), etc_report=etc
    )
    # Path authority is proven, but the measured record corroborates none
    # of the reflection guidance — it must not read as actionable.
    assert report.status == 'diagnostics_only'


def test_ambiguous_peak_emits_resolve_ambiguity() -> None:
    direct, reflection, request = _pair()
    reflection2 = _path(
        path_type='specular_reflection',
        length_m=7.0,
        delay_s=7.0 / 343.0,
        surface_id='wall-right',
        point=Position3(x_m=5.0, y_m=2.0, z_m=1.0),
    )
    etc = match_etc_peaks(
        request,
        (direct, reflection, reflection2),
        (5.0 / 343.0, 7.0 / 343.0),
        measured_timing=_timing_evidence(),
    )
    report = build_reflection_guidance(
        request, direct, (reflection, reflection2), etc_report=etc
    )
    ambiguity_items = [
        item
        for item in report.items
        if item.kind == 'resolve_ambiguity'
    ]
    assert len(ambiguity_items) == 1
    # The match itself names no path; the item must still name a real
    # competing surface recovered from the tolerance comparison.
    assert ambiguity_items[0].surface_id in {'wall-left', 'wall-right'}
    assert ambiguity_items[0].etc_report_id == etc.report_id


def test_report_invariants_fail_closed() -> None:
    direct, reflection, request = _pair()
    report = build_reflection_guidance(
        request, direct, (reflection,)
    )
    # Blocked reports cannot smuggle items.
    with pytest.raises(ValidationError):
        ReflectionGuidanceReport(
            request_id=report.request_id,
            request_semantic_sha256=report.request_semantic_sha256,
            scene_revision_id=report.scene_revision_id,
            scene_content_hash=report.scene_content_hash,
            status='blocked',
            items=report.items,
            report_id='reflection-guidance-report:' + '0' * 64,
            semantic_sha256='0' * 64,
        )
    # A request from a different diagnosis must not bind.
    other_request = _request(
        _path(path_type='direct', length_m=6.0, delay_s=6.0 / 343.0),
        reflection,
    )
    with pytest.raises(ValueError, match='does not belong'):
        build_reflection_guidance(
            request,
            direct,
            (reflection,),
            etc_report=match_etc_peaks(
                other_request,
                (direct, reflection),
                (7.0 / 343.0,),
                measured_timing=_timing_evidence(),
            ),
        )


def test_guidance_session_scrub_tracks_deltas() -> None:
    direct, reflection, request = _pair()
    session = open_guidance_session(
        request,
        direct,
        reflection,
        receiver_position=Position3(x_m=1.0, y_m=0.0, z_m=1.0),
        plane_point=Position3(x_m=3.0, y_m=0.0, z_m=0.0),
        plane_normal=(1.0, 0.0, 0.0),
    )
    assert session.baseline_excess_delay_s == pytest.approx(2.0 / 343.0)
    assert session.frames == ()

    # Moving the source away from the boundary lengthens the specular leg
    # relative to the direct — the move improves the excess delay.
    far = scrub_source(
        session, Position3(x_m=-1.0, y_m=2.0, z_m=1.0)
    )
    assert len(far.frames) == 1
    frame = far.frames[0]
    assert frame.direction == 'improves'
    assert frame.excess_delay_delta_s > 0.0
    assert frame.preview.excess_delay_s == pytest.approx(
        session.baseline_excess_delay_s + frame.excess_delay_delta_s
    )

    # Pushing the source toward the boundary shortens the specular leg
    # relative to the direct — the frame reports 'worsens'.
    near = scrub_source(
        session, Position3(x_m=2.5, y_m=2.0, z_m=1.0)
    )
    assert near.frames[-1].direction == 'worsens'
    assert near.frames[-1].excess_delay_delta_s < 0.0

    # The committed request/path authority never mutates across frames.
    assert far.request == session.request
    assert far.reflection_path == session.reflection_path
