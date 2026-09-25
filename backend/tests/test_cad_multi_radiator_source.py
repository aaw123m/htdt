"""Multi-radiator source authority tests (#1006)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_bass_management import FrequencyBand
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_multi_radiator_source import (
    MultiRadiatorSourceModel,
    SourceRadiatorElement,
    SourceReferencePoints,
    SourceTransferEdge,
    build_multi_radiator_source_model,
    evaluate_multi_radiator_coherence,
)
from htdt.cad_scene import Offset3


def _provenance(digit: str = '4'):
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Loudspeaker Co.',
            source_version='1',
            source_reference='element datasheet',
            source_sha256=digit * 64,
        ),
    )


def _element(element_id: str, **overrides):
    kwargs = dict(
        kind='driver',
        local_position_m=Offset3(x_m=0.0, y_m=0.1, z_m=0.0),
        valid_band=FrequencyBand(low_hz=800.0, high_hz=20000.0),
        transfer_evidence='complex',
        transfer_dataset_ref='dataset-tweeter-1',
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return SourceRadiatorElement(element_id=element_id, **kwargs)


def _model(**overrides):
    kwargs = dict(
        model_id='model-3way',
        version='1',
        equipment_definition_id='spk-1',
        equipment_definition_version='1',
        equipment_definition_sha256='9' * 64,
        elements=(
            _element(
                'woofer',
                valid_band=FrequencyBand(low_hz=30.0, high_hz=800.0),
                transfer_dataset_ref='dataset-woofer-1',
                local_position_m=Offset3(x_m=0.0, y_m=-0.2, z_m=0.0),
            ),
            _element('tweeter'),
        ),
        transfer_edges=(
            SourceTransferEdge(
                element_id='woofer',
                gain_db=0.0,
                delay_ms=0.0,
                polarity='normal',
                filter_refs=('xover-lp-800',),
            ),
            SourceTransferEdge(
                element_id='tweeter',
                gain_db=-2.0,
                delay_ms=0.15,
                polarity='normal',
                filter_refs=('xover-hp-800',),
            ),
        ),
        reference_points=SourceReferencePoints(
            geometric_reference_m=Offset3(),
            dataset_phase_origin_m=Offset3(),
            dataset_phase_origin_ref='dataset-phase-1',
        ),
        provenance=_provenance('9'),
    )
    kwargs.update(overrides)
    return build_multi_radiator_source_model(**kwargs)


def test_model_hash_and_edges_resolve():
    model = _model()
    assert model.semantic_sha256
    payload = model.model_dump(mode='python')
    payload['whole_system_directivity_ref'] = 'ds-1'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        MultiRadiatorSourceModel(**payload)
    with pytest.raises(ValidationError, match='unknown element'):
        _model(
            transfer_edges=(SourceTransferEdge(element_id='ghost'),)
        )


def test_reference_frames_stay_distinct():
    model = _model()
    points = model.reference_points
    assert points.geometric_reference_m is not None
    assert points.dataset_phase_origin_m is not None
    # distinct frames — absence is None, never borrowed
    assert points.effective_acoustic_center_m is None
    assert points.solver_equivalent_point_m is None
    with pytest.raises(ValidationError, match='together'):
        SourceReferencePoints(dataset_phase_origin_m=Offset3())


def test_element_evidence_requires_dataset_binding():
    with pytest.raises(ValidationError, match='dataset reference'):
        _element('x', transfer_evidence='complex', transfer_dataset_ref=None)
    with pytest.raises(ValidationError, match='evidence kind'):
        _element('x', transfer_evidence='none',
                 transfer_dataset_ref='ds-1')


def test_coherent_summation_supported():
    evaluation = evaluate_multi_radiator_coherence(model=_model())
    assert evaluation.coherent_summation_supported is True
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['transfer_evidence_complex'] == 'PASS'
    assert checks['phase_origin_shared'] == 'PASS'
    assert checks['double_counting_guard'] == 'PASS'


def test_magnitude_only_elements_cannot_coherently_sum():
    model = _model(
        elements=(
            _element('woofer', transfer_evidence='magnitude_only'),
            _element('tweeter'),
        )
    )
    evaluation = evaluate_multi_radiator_coherence(model=model)
    assert evaluation.coherent_summation_supported is False
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['transfer_evidence_complex'] == 'UNKNOWN'


def test_overlapping_bands_without_crossover_flagged():
    model = _model(
        elements=(
            _element('a', valid_band=FrequencyBand(low_hz=100.0, high_hz=1000.0)),
            _element('b', valid_band=FrequencyBand(low_hz=500.0, high_hz=2000.0)),
        ),
        transfer_edges=(SourceTransferEdge(element_id='a'),),
    )
    evaluation = evaluate_multi_radiator_coherence(model=model)
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['double_counting_guard'] == 'UNKNOWN'
    assert evaluation.coherent_summation_supported is False


def test_whole_system_directivity_stays_valid_alone():
    model = _model(whole_system_directivity_ref='dataset-system-1')
    evaluation = evaluate_multi_radiator_coherence(model=model)
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['whole_system_directivity_valid'] == 'PASS'
    # does not depend on per-element decomposition
    assert model.whole_system_directivity_ref == 'dataset-system-1'
