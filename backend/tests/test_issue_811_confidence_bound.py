"""Issue #811 regression tests — solver confidence bounded by material,
source-directivity, geometry and pose input authority.

A high-fidelity solver with weak boundary/source/geometry inputs must
not inherit a high-confidence result: the claimable confidence is the
MINIMUM over declared input authority classes and the solver validation
domain, per claim class, with the weakest dimension named. Undeclared
inputs fail closed to ``unbounded_input``/``insufficient_authority`` —
never a mid-trust default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.acoustic_validation_envelope import (
    build_accuracy_envelope,
    error_statistic,
)
from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_solver_capability_manifest import (
    SOLVER_PATH_PHENOMENA,
    SolverCapabilityRow,
    build_solver_capability_manifest,
)
from htdt.cad_solver_confidence_bound import (
    CLAIM_CLASSES,
    CONFIDENCE_BOUND_LABELS,
    ClaimBoundRecord,
    ClaimBoundRow,
    EnvironmentalBinding,
    GeometryInputAuthority,
    MaterialInputAuthority,
    PoseInputAuthority,
    SolverInputEnvelope,
    SourceDirectivityAuthority,
    evaluate_claim_bound,
    evaluate_input_envelope,
)
from htdt.cad_solver_confidence_bound_repository import (
    CadSolverConfidenceBoundRepository,
    ConfidenceBoundConflictError,
    ConfidenceBoundIntegrityError,
)
from htdt.canonical_json import canonical_sha256
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


DOC = 'doc-issue811'
_SHA = canonical_sha256({'fixture': 'sha'})
_SHA2 = canonical_sha256({'fixture': 'sha2'})
_SHA3 = canonical_sha256({'fixture': 'sha3'})
_SHA4 = canonical_sha256({'fixture': 'sha4'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _unpinned_ref(kind: str, rid: str = 'x1') -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=None)


def _xref(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version='1',
        semantic_hash_sha256='ab' * 32,
    )


def _material(**kw) -> MaterialInputAuthority:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('boundary', 'wall-1'),
        authority_class='measured_complex_boundary',
        quantity='surface_impedance',
        method_class='iso_10534_2_impedance_tube',
        phase='complex_impedance',
        incidence='diffuse_reverberant',
        frequency_validity_hz=(50.0, 5000.0),
        evidence_refs=(_ref('material_evidence', 'me-1'),),
    )
    payload.update(kw)
    return MaterialInputAuthority.create(**payload)


def _scalar_material(**kw) -> MaterialInputAuthority:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('boundary', 'wall-2'),
        authority_class='scalar_absorption_only',
        quantity='absorption_coefficient_diffuse_field',
        method_class='database_reference',
        phase='magnitude_energy_only',
        incidence='diffuse_reverberant',
    )
    payload.update(kw)
    return MaterialInputAuthority.create(**payload)


def _directivity(**kw) -> SourceDirectivityAuthority:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('source', 'spk-1'),
        provenance_class='measured_balloon',
        dataset_ref=_ref('directivity_dataset', 'dd-1'),
        format_compliance='sofa_aes69',
        coverage='full_sphere',
        angular_sampling='5 deg grid',
        frequency_sampling='1/3-oct',
        interpolation_method='nearest_band',
        coordinate_frame='sofa_spherical',
        normalization_reference='on_axis_1m',
    )
    payload.update(kw)
    return SourceDirectivityAuthority.create(**payload)


def _omni_directivity(**kw) -> SourceDirectivityAuthority:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('source', 'spk-2'),
        provenance_class='simplified_model',
        simplified_model='omnidirectional',
    )
    payload.update(kw)
    return SourceDirectivityAuthority.create(**payload)


def _geometry(**kw) -> GeometryInputAuthority:
    payload = dict(
        document_id=DOC,
        fidelity_class='exact_scene_revision',
        scene_revision_ref=_ref('scene_revision', 'sr-1'),
    )
    payload.update(kw)
    return GeometryInputAuthority.create(**payload)


def _pose(**kw) -> PoseInputAuthority:
    payload = dict(
        document_id=DOC,
        subject_kind='source',
        subject_ref=_ref('source', 'spk-1'),
        authority_class='surveyed_acoustic_center',
        acoustic_center_ref=_ref('source_origin', 'so-1'),
        position_bound_m=0.05,
        modal_sensitivity='evaluated',
    )
    payload.update(kw)
    return PoseInputAuthority.create(**payload)


def _environment(**states) -> tuple[EnvironmentalBinding, ...]:
    default = {
        'temperature': 'declared',
        'humidity': 'declared',
        'speed_of_sound': 'declared',
        'door_opening_state': 'declared',
        'movable_objects': 'declared',
        'playback_state': 'declared',
    }
    default.update(states)
    return tuple(
        EnvironmentalBinding(aspect=aspect, state=state)  # type: ignore[arg-type]
        for aspect, state in default.items()
    )


def _measured_environment() -> tuple[EnvironmentalBinding, ...]:
    return tuple(
        EnvironmentalBinding(
            aspect=aspect,  # type: ignore[arg-type]
            state='measured',
            value='surveyed',
        )
        for aspect in (
            'temperature', 'humidity', 'speed_of_sound',
            'door_opening_state', 'movable_objects', 'playback_state',
        )
    )


def _envelope_for(
    observable: str,
    state: str = 'VALIDATED_FOR_DECLARED_DOMAIN',
    solver_version: str = '2.0',
):
    return build_accuracy_envelope(
        solver_id='wave-solver',
        solver_algorithm='fdtd',
        solver_version=solver_version,
        observable=observable,  # type: ignore[arg-type]
        geometry_domain='closed room',
        boundary_material_assumptions='complex impedance',
        source_capability='measured balloon',
        receiver_capability='mic array',
        fixture_ids=('fx-1',),
        fixture_sha256s=(_SHA,),
        error_statistic_definition='absolute error in dB',
        error_distribution=error_statistic(
            observable, 'dB', (0.1, -0.2, 0.15)  # type: ignore[arg-type]
        ),
        threshold_policy_id='policy-1',
        threshold_policy_revision=2,
        validation_state=state,  # type: ignore[arg-type]
        validated_at_utc='2026-10-07T00:00:00Z',
    )


def _full_envelopes(
    state: str = 'VALIDATED_FOR_DECLARED_DOMAIN',
):
    return tuple(
        _envelope_for(observable, state)
        for observable in (
            'spatial_field_db',
            'seat_to_seat_variation_db',
            'magnitude_response_db',
            'reflection_arrival_time_s',
            'modal_frequency_hz',
            'decay_time_s',
            'phase_deg',
            'hybrid_overlap_level_db',
        )
    )


def _manifest(*, unsupported: tuple[str, ...] = ()):
    descriptor = build_acoustic_solver_adapter_descriptor(
        adapter_id='htdt.fdtd-wave',
        adapter_version='2.0',
        model_solver_role_id='wave-solver',
        acoustic_domain='wave',
        solver_implementation_ref=_xref('fdtd-impl'),
        solver_configuration_schema_ref=_xref('fdtd-config'),
        supported_snapshot_schema_versions=(1,),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=5000.0,
        ),
    )
    rows = []
    for phenomenon in SOLVER_PATH_PHENOMENA:
        if phenomenon in unsupported:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,  # type: ignore[arg-type]
                    state='UNSUPPORTED',
                    reasons=(f'{phenomenon} not modeled on this path',),
                )
            )
        else:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,  # type: ignore[arg-type]
                    state='SUPPORTED',
                    valid_frequency_domain=FrequencyDomain(
                        minimum_hz=20.0, maximum_hz=5000.0,
                    ),
                )
            )
    return build_solver_capability_manifest(
        descriptor=descriptor, rows=tuple(rows),
    )


def _input_envelope(
    records=(),
    *,
    materials=(),
    directivities=(),
    geometries=(),
    poses=(),
    environment=None,
    **kw,
) -> SolverInputEnvelope:
    payload = dict(
        document_id=DOC,
        solver_request_ref=_ref('solver_request', 'req-1'),
        material_refs=tuple(
            AuthorityRef(
                kind='material_input_authority',
                ref_id=record.material_id,
                ref_sha256=record.material_sha256,
            )
            for record in materials
        ),
        directivity_refs=tuple(
            AuthorityRef(
                kind='source_directivity_authority',
                ref_id=record.directivity_id,
                ref_sha256=record.directivity_sha256,
            )
            for record in directivities
        ),
        geometry_refs=tuple(
            AuthorityRef(
                kind='geometry_input_authority',
                ref_id=record.geometry_id,
                ref_sha256=record.geometry_sha256,
            )
            for record in geometries
        ),
        pose_refs=tuple(
            AuthorityRef(
                kind='pose_input_authority',
                ref_id=record.pose_id,
                ref_sha256=record.pose_sha256,
            )
            for record in poses
        ),
        environment=environment if environment is not None else _environment(),
        declared_at_utc='2026-10-07T00:00:00Z',
    )
    payload.update(kw)
    return SolverInputEnvelope.create(**payload)


def _bound_row(claim: str, verdict: str = 'envelope_inherited',
               ceiling: str = 'full_envelope', **kw) -> ClaimBoundRow:
    payload = dict(
        claim=claim,
        verdict=verdict,
        ceiling=ceiling,
        weakest_dimensions=(),
        reasons=(),
        supporting_refs=(),
    )
    payload.update(kw)
    return ClaimBoundRow(**payload)


def _bound_record(envelope: SolverInputEnvelope, rows=None) -> ClaimBoundRecord:
    if rows is None:
        rows = tuple(
            _bound_row(claim) for claim in sorted(CLAIM_CLASSES)
        )
    return ClaimBoundRecord.create(
        document_id=DOC,
        input_envelope_ref=_ref(
            'solver_input_envelope',
            envelope.envelope_id,
            envelope.envelope_sha256,
        ),
        rows=rows,
        evaluated_at_utc='2026-10-07T00:05:00Z',
    )


# ---------------------------------------------------------------------------
# Sealed record validation
# ---------------------------------------------------------------------------


def test_material_authority_seal_is_deterministic() -> None:
    record = _material()
    assert record.material_id.startswith('mia-')
    assert record.material_sha256 == canonical_sha256(
        record.identity_payload()
    )
    again = _material()
    assert again.material_id == record.material_id
    assert again.material_sha256 == record.material_sha256


def test_material_authority_hash_and_id_are_checked() -> None:
    record = _material()
    with pytest.raises(ValidationError):
        MaterialInputAuthority(
            **{**record.model_dump(mode='python'),
               'material_sha256': _SHA2}
        )
    with pytest.raises(ValidationError):
        MaterialInputAuthority(
            **{**record.model_dump(mode='python'),
               'material_id': 'mia-' + 'f' * 24}
        )


def test_material_authority_unpinned_refs_fail() -> None:
    with pytest.raises(ValidationError):
        _material(subject_ref=_unpinned_ref('boundary'))
    with pytest.raises(ValidationError):
        _material(evidence_refs=(_unpinned_ref('material_evidence'),))


def test_material_measured_classes_require_evidence() -> None:
    for cls in (
        'measured_complex_boundary',
        'measured_parameterized_model',
        'manufacturer_laboratory_exact',
        'measured_energy_coefficient',
    ):
        with pytest.raises(ValidationError):
            _material(
                authority_class=cls,
                quantity=(
                    'surface_impedance'
                    if cls == 'measured_complex_boundary'
                    else 'absorption_coefficient_diffuse_field'
                ),
                method_class='iso_354_reverberation_room',
                phase=(
                    'complex_impedance'
                    if cls == 'measured_complex_boundary'
                    else 'magnitude_energy_only'
                ),
                evidence_refs=(),
                laboratory='lab-1' if cls == 'manufacturer_laboratory_exact' else None,
                uncertainty_declared=cls == 'manufacturer_laboratory_exact',
            )


def test_material_fitted_context_marks_fitted_provenance() -> None:
    # A calibrated value cannot wear a measured class label (issue §7).
    with pytest.raises(ValidationError):
        _material(
            authority_class='measured_complex_boundary',
            fitted_context='calibrated against session #44',
        )
    with pytest.raises(ValidationError):
        _material(
            authority_class='fitted_effective',
            quantity='absorption_coefficient_diffuse_field',
            method_class='inverse_estimated',
            phase='magnitude_energy_only',
            evidence_refs=(),
            frequency_validity_hz=None,
            fitted_context=None,
        )


def test_fitted_effective_is_distinguishable_from_measured() -> None:
    record = _material(
        authority_class='fitted_effective',
        quantity='absorption_coefficient_diffuse_field',
        method_class='inverse_estimated',
        phase='magnitude_energy_only',
        evidence_refs=(),
        frequency_validity_hz=None,
        fitted_context='calibrated against session #44',
    )
    assert record.authority_class == 'fitted_effective'
    assert record.fitted_context == 'calibrated against session #44'


def test_material_derived_conversion_requires_artifact() -> None:
    with pytest.raises(ValidationError):
        _material(
            authority_class='derived_conversion',
            quantity='surface_impedance',
            method_class='derived_conversion',
            phase='derived_complex_model',
            evidence_refs=(),
            frequency_validity_hz=None,
        )
    # a measured class cannot launder a conversion product
    with pytest.raises(ValidationError):
        _material(conversion_ref=_ref('boundary_conversion', 'cv-1'))


def test_material_derived_conversion_pins_artifact() -> None:
    record = _material(
        authority_class='derived_conversion',
        quantity='surface_impedance',
        method_class='derived_conversion',
        phase='derived_complex_model',
        evidence_refs=(),
        frequency_validity_hz=None,
        conversion_ref=_ref('boundary_conversion', 'cv-1'),
    )
    assert record.conversion_ref is not None
    assert record.conversion_ref.ref_id == 'cv-1'


def test_material_undeclared_cannot_pin_evidence() -> None:
    with pytest.raises(ValidationError):
        _material(
            authority_class='undeclared',
            quantity='unknown_quantity',
            method_class='unknown_method',
            phase='unknown_phase',
            incidence='unknown',
            evidence_refs=(_ref('material_evidence'),),
            frequency_validity_hz=None,
        )


def test_directivity_measured_balloon_requires_dataset_and_coverage() -> None:
    with pytest.raises(ValidationError):
        _directivity(dataset_ref=None)
    with pytest.raises(ValidationError):
        _directivity(coverage='unknown')
    with pytest.raises(ValidationError):
        _directivity(angular_sampling=None)


def test_directivity_simplified_model_requires_named_model() -> None:
    with pytest.raises(ValidationError):
        _omni_directivity(simplified_model=None)
    with pytest.raises(ValidationError):
        _omni_directivity(dataset_ref=_ref('directivity_dataset'))


def test_directivity_fitted_context_stays_fitted() -> None:
    with pytest.raises(ValidationError):
        _directivity(fitted_context='tuned to session #9')
    record = _directivity(
        provenance_class='fitted_source_correction',
        fitted_context='calibrated against measurement session #9',
    )
    assert record.provenance_class == 'fitted_source_correction'


def test_directivity_seal_and_id() -> None:
    record = _directivity()
    assert record.directivity_id.startswith('sda-')
    assert record.directivity_sha256 == canonical_sha256(
        record.identity_payload()
    )
    with pytest.raises(ValidationError):
        SourceDirectivityAuthority(
            **{**record.model_dump(mode='python'),
               'directivity_sha256': _SHA2}
        )


def test_geometry_exact_requires_scene_revision() -> None:
    with pytest.raises(ValidationError):
        _geometry(scene_revision_ref=None)
    with pytest.raises(ValidationError):
        _geometry(
            fidelity_class='simplified',
            scene_revision_ref=None,
        )


def test_geometry_simplified_requires_policy() -> None:
    record = _geometry(
        fidelity_class='simplified',
        scene_revision_ref=None,
        detail_policy='furniture below 50 mm dropped',
    )
    assert record.fidelity_class == 'simplified'
    # an undeclared geometry input cannot pin a scene revision
    with pytest.raises(ValidationError):
        _geometry(
            fidelity_class='undeclared',
            scene_revision_ref=_ref('scene_revision', 'sr-1'),
        )


def test_pose_surveyed_requires_center_and_bound() -> None:
    with pytest.raises(ValidationError):
        _pose(acoustic_center_ref=None)
    with pytest.raises(ValidationError):
        _pose(position_bound_m=None)
    with pytest.raises(ValidationError):
        _pose(
            authority_class='assumed',
            acoustic_center_ref=None,
            position_bound_m=None,
            pose_observation_ref=_ref('pose_observation'),
        )


def test_pose_modal_evaluation_requires_bound() -> None:
    with pytest.raises(ValidationError):
        _pose(modal_sensitivity='evaluated', position_bound_m=None)


def test_input_envelope_rejects_duplicate_refs() -> None:
    material = _material()
    with pytest.raises(ValidationError):
        _input_envelope(materials=(material, material))


def test_input_envelope_rejects_duplicate_environment_aspects() -> None:
    with pytest.raises(ValidationError):
        _input_envelope(
            environment=(
                EnvironmentalBinding(aspect='temperature', state='declared'),
                EnvironmentalBinding(aspect='temperature', state='measured',
                                     value='20 C'),
            )
        )


def test_environmental_measured_binding_requires_value() -> None:
    with pytest.raises(ValidationError):
        EnvironmentalBinding(aspect='temperature', state='measured')
    with pytest.raises(ValidationError):
        EnvironmentalBinding(
            aspect='temperature', state='undeclared', value='20 C',
        )


def test_claim_bound_record_covers_every_claim_once() -> None:
    envelope = _input_envelope()
    rows = tuple(
        _bound_row(claim)
        for claim in sorted(CLAIM_CLASSES)
        if claim != 'decay_time'
    )
    with pytest.raises(ValidationError):
        _bound_record(envelope, rows=rows)
    duplicated = tuple(
        _bound_row(claim) for claim in sorted(CLAIM_CLASSES)
    ) + (_bound_row('decay_time'),)
    with pytest.raises(ValidationError):
        _bound_record(envelope, rows=duplicated)


def test_claim_bound_row_verdict_ceiling_consistency() -> None:
    with pytest.raises(ValidationError):
        _bound_row('decay_time', verdict='envelope_inherited',
                   ceiling='limited_bound')
    with pytest.raises(ValidationError):
        _bound_row('decay_time', verdict='bounded_by_input',
                   ceiling='full_envelope')
    with pytest.raises(ValidationError):
        _bound_row('decay_time', verdict='unbounded_input',
                   ceiling='limited_bound',
                   weakest_dimensions=('material_boundary',))
    with pytest.raises(ValidationError):
        _bound_row('decay_time', verdict='claim_denied',
                   weakest_dimensions=('material_boundary',))


# ---------------------------------------------------------------------------
# Evaluator — every verdict path, fail-closed first
# ---------------------------------------------------------------------------


def test_undeclared_material_fails_closed() -> None:
    row = evaluate_claim_bound(
        'absolute_level_at_position',
        materials=None,
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(),
    )
    assert row.verdict == 'unbounded_input'
    assert row.ceiling == 'insufficient_authority'
    assert 'material_boundary' in row.weakest_dimensions


def test_undeclared_pose_fails_closed() -> None:
    row = evaluate_claim_bound(
        'local_frequency_response',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=None,
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
    )
    assert row.verdict == 'unbounded_input'
    assert 'pose' in row.weakest_dimensions


def test_undeclared_environmental_aspect_stales_only_dependent_claims() -> None:
    # Issue §6: a door-state change stales claims that depend on door
    # state — never decay-time evidence (humidity/temperature aspect).
    sti = evaluate_claim_bound(
        'speech_intelligibility',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(door_opening_state='undeclared'),
        manifest=_manifest(),
    )
    decay = evaluate_claim_bound(
        'decay_time',
        materials=(_material(),),
        directivities=None,
        geometries=(_geometry(),),
        poses=None,
        environment=_environment(door_opening_state='undeclared'),
        solver_envelopes=_full_envelopes(),
    )
    assert sti.verdict == 'unbounded_input'
    assert 'environment' in sti.weakest_dimensions
    assert decay.verdict != 'unbounded_input'


def test_scalar_absorption_denies_phase_claim() -> None:
    # Issue case A: wave solver validated for complex impedance, only
    # scalar catalogue absorption — complex-field claims stay denied.
    row = evaluate_claim_bound(
        'phase_coherent_field',
        materials=(_scalar_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(),
    )
    assert row.verdict == 'claim_denied'
    assert row.ceiling == 'insufficient_authority'
    assert 'material_boundary' in row.weakest_dimensions


def test_omni_directivity_bounds_spatial_coverage() -> None:
    # Issue case B: benchmarked GA + generic omni directivity — SPL/
    # coverage/orientation claims are limited, never inherited.
    row = evaluate_claim_bound(
        'spatial_coverage',
        materials=(_material(),),
        directivities=(_omni_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(),
    )
    assert row.verdict == 'bounded_by_input'
    assert row.ceiling == 'assumed_bound'
    assert row.weakest_dimensions == ('source_directivity',)


def test_solver_without_evidence_is_unqualified() -> None:
    row = evaluate_claim_bound(
        'decay_time',
        materials=(_material(),),
        directivities=None,
        geometries=(_geometry(),),
        poses=None,
        environment=_environment(),
    )
    assert row.verdict == 'solver_unqualified'
    assert row.ceiling == 'insufficient_authority'
    assert row.weakest_dimensions == ('solver_domain',)


def test_manifest_unsupported_is_unqualified() -> None:
    row = evaluate_claim_bound(
        'modal_response',
        materials=(_material(),),
        directivities=None,
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(
            unsupported=('low_frequency_modal_response',),
        ),
    )
    assert row.verdict == 'solver_unqualified'
    assert any('UNSUPPORTED' in reason for reason in row.reasons)


def test_missing_observable_envelope_is_unqualified() -> None:
    # An envelope for another observable never covers this claim (§8).
    row = evaluate_claim_bound(
        'decay_time',
        materials=(_material(),),
        directivities=None,
        geometries=(_geometry(),),
        poses=None,
        environment=_environment(),
        solver_envelopes=(_envelope_for('spatial_field_db'),),
    )
    assert row.verdict == 'solver_unqualified'
    assert any('decay_time_s' in reason for reason in row.reasons)


def test_limited_solver_envelope_bounds_claim() -> None:
    row = evaluate_claim_bound(
        'absolute_level_at_position',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_measured_environment(),
        solver_envelopes=_full_envelopes('VALIDATED_WITH_LIMITATIONS'),
    )
    assert row.verdict == 'solver_bounded'
    assert row.ceiling == 'solver_bound'
    assert row.weakest_dimensions == ('solver_domain',)


def test_full_envelope_inheritance() -> None:
    row = evaluate_claim_bound(
        'phase_coherent_field',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_measured_environment(),
        solver_envelopes=_full_envelopes(),
    )
    assert row.verdict == 'envelope_inherited'
    assert row.ceiling == 'full_envelope'
    assert row.weakest_dimensions == ()


def test_modal_sensitive_claim_bounds_unevaluated_pose() -> None:
    # Issue case C: uncertain mic position vs modal gradient — absolute
    # local FR bounded by spatial uncertainty while robust aggregates
    # remain usable.
    pose = _pose(modal_sensitivity='unevaluated')
    fr = evaluate_claim_bound(
        'local_frequency_response',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(pose,),
        environment=_measured_environment(),
        solver_envelopes=_full_envelopes(),
    )
    coverage = evaluate_claim_bound(
        'spatial_coverage',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(pose,),
        environment=_measured_environment(),
        solver_envelopes=_full_envelopes(),
    )
    assert fr.verdict == 'bounded_by_input'
    assert 'pose' in fr.weakest_dimensions
    assert any('modal' in reason for reason in fr.reasons)
    assert coverage.verdict == 'envelope_inherited'


def test_weakest_input_is_identified_across_dimensions() -> None:
    row = evaluate_claim_bound(
        'early_reflection_structure',
        materials=(_scalar_material(),),
        directivities=(_omni_directivity(),),
        geometries=(_geometry(),),
        poses=(_pose(),),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(),
    )
    assert row.verdict == 'bounded_by_input'
    assert row.ceiling == 'assumed_bound'
    assert set(row.weakest_dimensions) == {
        'material_boundary', 'source_directivity',
    }


def test_assumed_pose_denies_at_position_claim() -> None:
    row = evaluate_claim_bound(
        'absolute_level_at_position',
        materials=(_material(),),
        directivities=(_directivity(),),
        geometries=(_geometry(),),
        poses=(
            _pose(
                authority_class='assumed',
                acoustic_center_ref=None,
                position_bound_m=None,
                modal_sensitivity='unevaluated',
            ),
        ),
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
    )
    assert row.verdict == 'claim_denied'
    assert 'pose' in row.weakest_dimensions


def test_fitted_material_is_not_measured() -> None:
    # A calibrated absorption feeds bounded energy claims, never the
    # measured-complex-boundary class (issue §7).
    fitted = _material(
        authority_class='fitted_effective',
        quantity='absorption_coefficient_diffuse_field',
        method_class='inverse_estimated',
        phase='magnitude_energy_only',
        evidence_refs=(),
        frequency_validity_hz=None,
        fitted_context='calibrated against session #44',
    )
    row = evaluate_claim_bound(
        'decay_time',
        materials=(fitted,),
        directivities=None,
        geometries=(_geometry(),),
        poses=None,
        environment=_environment(),
        solver_envelopes=_full_envelopes(),
    )
    assert row.verdict == 'bounded_by_input'
    assert row.ceiling == 'limited_bound'
    assert 'material_boundary' in row.weakest_dimensions


# ---------------------------------------------------------------------------
# Envelope evaluation + sealed claim bound record
# ---------------------------------------------------------------------------


def test_evaluate_input_envelope_seals_per_claim_record() -> None:
    material = _material()
    directivity = _directivity()
    geometry = _geometry()
    pose = _pose()
    envelope = _input_envelope(
        materials=(material,),
        directivities=(directivity,),
        geometries=(geometry,),
        poses=(pose,),
        environment=_measured_environment(),
    )
    record = evaluate_input_envelope(
        envelope,
        materials=(material,),
        directivities=(directivity,),
        geometries=(geometry,),
        poses=(pose,),
        solver_envelopes=_full_envelopes(),
        manifest=_manifest(),
        evaluated_at_utc='2026-10-07T00:05:00Z',
    )
    assert record.record_id.startswith('cbr-')
    assert record.record_sha256 == canonical_sha256(
        record.identity_payload()
    )
    assert len(record.rows) == len(CLAIM_CLASSES)
    assert {
        row.claim for row in record.rows
    } == set(CLAIM_CLASSES)
    for row in record.rows:
        if row.claim == 'late_diffuse_field':
            # complex-boundary impedance evidence is not ISO 17497-1
            # scattering evidence — the diffuse-field claim stays bounded.
            assert row.verdict == 'bounded_by_input'
            assert row.ceiling == 'limited_bound'
            assert 'material_boundary' in row.weakest_dimensions
        elif row.claim == 'speech_intelligibility':
            # no STI observable envelope exists — the manifest's
            # documented spatial-field capability sets the bound.
            assert row.verdict == 'solver_bounded'
            assert row.ceiling == 'solver_bound'
            assert row.weakest_dimensions == ('solver_domain',)
        else:
            assert row.verdict == 'envelope_inherited'
            assert row.ceiling == 'full_envelope'


def test_evaluate_input_envelope_rejects_sha_mismatch() -> None:
    material = _material()
    envelope = _input_envelope(materials=(material,))
    impostor = _scalar_material()
    with pytest.raises(ValueError):
        evaluate_input_envelope(
            envelope,
            materials=(impostor,),  # different id — never resolves
            evaluated_at_utc='2026-10-07T00:05:00Z',
        )
    with pytest.raises(ValueError):
        evaluate_input_envelope(
            envelope,
            materials=(_material(subject_ref=_ref('boundary', 'wall-9')),),
            evaluated_at_utc='2026-10-07T00:05:00Z',
        )


def test_evaluate_input_envelope_missing_record_fails() -> None:
    material = _material()
    envelope = _input_envelope(materials=(material,))
    with pytest.raises(ValueError):
        evaluate_input_envelope(
            envelope,
            materials=(),
            evaluated_at_utc='2026-10-07T00:05:00Z',
        )


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def _repository(tmp_path: Path) -> CadSolverConfidenceBoundRepository:
    path = tmp_path / 'cad.sqlite3'
    ensure_native_schema(path)
    return CadSolverConfidenceBoundRepository(SceneRepository(path))


def test_repository_round_trip_all_stores(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    material = _material()
    directivity = _directivity()
    geometry = _geometry()
    pose = _pose()
    envelope = _input_envelope(
        materials=(material,),
        directivities=(directivity,),
        geometries=(geometry,),
        poses=(pose,),
    )
    bound = _bound_record(envelope)

    repo.save_material_authority(material)
    repo.save_directivity_authority(directivity)
    repo.save_geometry_authority(geometry)
    repo.save_pose_authority(pose)
    repo.save_input_envelope(envelope)
    repo.save_claim_bound(bound)

    assert repo.get_material_authority(material.material_id) == material
    assert (
        repo.get_directivity_authority(directivity.directivity_id)
        == directivity
    )
    assert repo.get_geometry_authority(geometry.geometry_id) == geometry
    assert repo.get_pose_authority(pose.pose_id) == pose
    assert repo.get_input_envelope(envelope.envelope_id) == envelope
    assert repo.get_claim_bound(bound.record_id) == bound

    assert len(repo.list_material_authorities(DOC)) == 1
    assert len(repo.list_directivity_authorities(DOC)) == 1
    assert len(repo.list_geometry_authorities(DOC)) == 1
    assert len(repo.list_pose_authorities(DOC)) == 1
    assert len(repo.list_input_envelopes(DOC)) == 1
    assert len(repo.list_claim_bounds(DOC)) == 1
    assert repo.list_material_authorities('other-doc') == ()


def test_repository_append_only_conflict(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    material = _material()
    repo.save_material_authority(material)
    repo.save_material_authority(material)  # same record: idempotent
    other = _material(subject_ref=_ref('boundary', 'wall-9'))
    repo.save_material_authority(other)
    tampered = MaterialInputAuthority.model_construct(
        **{**material.model_dump(mode='python'),
           'authority_class': 'manufacturer_declared'}
    )
    with pytest.raises(ConfidenceBoundIntegrityError):
        repo.save_material_authority(tampered)


def test_repository_detects_column_tampering(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    material = _material()
    repo.save_material_authority(material)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_material_input_authorities '
            "SET authority_class='manufacturer_declared' "
            'WHERE material_id=?',
            (material.material_id,),
        )
        connection.commit()
    with pytest.raises(ConfidenceBoundIntegrityError):
        repo.get_material_authority(material.material_id)


def test_repository_detects_payload_tampering(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    envelope = _input_envelope()
    repo.save_input_envelope(envelope)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_solver_input_envelopes '
            'SET solver_request_ref_id=? WHERE envelope_id=?',
            ('solver_request-forged', envelope.envelope_id),
        )
        connection.commit()
    with pytest.raises(ConfidenceBoundIntegrityError):
        repo.get_input_envelope(envelope.envelope_id)


# ---------------------------------------------------------------------------
# Labels — product-facing JA vocabulary is present
# ---------------------------------------------------------------------------


def test_ja_labels_cover_verdicts_ceilings_and_dimensions() -> None:
    for verdict in (
        'envelope_inherited', 'solver_bounded', 'bounded_by_input',
        'claim_denied', 'solver_unqualified', 'unbounded_input',
    ):
        assert verdict in CONFIDENCE_BOUND_LABELS
    for ceiling in (
        'full_envelope', 'solver_bound', 'measured_bound',
        'documented_bound', 'limited_bound', 'derived_bound',
        'assumed_bound', 'insufficient_authority',
    ):
        assert ceiling in CONFIDENCE_BOUND_LABELS
    for dimension in (
        'material_boundary', 'source_directivity', 'geometry',
        'pose', 'environment', 'solver_domain',
    ):
        assert dimension in CONFIDENCE_BOUND_LABELS
    for cls in ('fitted_effective', 'undeclared'):
        assert cls in CONFIDENCE_BOUND_LABELS
