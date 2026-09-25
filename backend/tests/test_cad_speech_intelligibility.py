from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_speech_intelligibility import (
    StiAuthorityRef,
    StiImpulseEvidenceRef,
    StiProducerRef,
    build_speech_intelligibility_spec,
    import_sti_producer_report,
)


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _authority(label: str) -> StiAuthorityRef:
    return StiAuthorityRef(
        authority_id=f'{label}-1', authority_sha256=_hash(label)
    )


def _spec(**overrides):
    kwargs = dict(
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        source_routing_identity=_authority('routing'),
        impulse_evidence=StiImpulseEvidenceRef(
            kind='measured',
            artifact_id='ir:seat-1',
            artifact_sha256=_hash('ir'),
            sample_rate_hz=48000,
            receiver_id='seat-1',
            receiver_entity_id='entity-seat-1',
            mic_applicability_authority=_authority('mic'),
        ),
        noise_authority=_authority('noise'),
        absolute_level_authority=_authority('absolute-spl'),
        producer=StiProducerRef(
            producer='rew',
            producer_version='5.31.3',
            producer_build='build-1',
            method='iec_60268_16_2020',
            report_artifact_id='rew-report:1',
            report_sha256=_hash('rew-report'),
            producer_parameters_recoverable=True,
        ),
        seat_ids=('seat-1', 'seat-2'),
    )
    kwargs.update(overrides)
    return build_speech_intelligibility_spec(**kwargs)


def test_spec_identity_and_authority_pins() -> None:
    spec = _spec()
    assert spec.spec_id.startswith('speech-intelligibility-spec:')
    # Every authority the import depends on is pinned on the spec.
    assert spec.noise_authority.authority_id == 'noise-1'
    assert spec.absolute_level_authority.authority_id == 'absolute-spl-1'
    assert spec.impulse_evidence.artifact_sha256 == _hash('ir')
    with pytest.raises(ValueError):
        _spec(seat_ids=('seat-1', 'seat-1'))


def test_import_records_raw_per_seat_values() -> None:
    spec = _spec()
    result = import_sti_producer_report(
        spec, per_seat_values={'seat-1': 0.72, 'seat-2': 0.45}
    )
    assert result.result_id.startswith('speech-intelligibility-result:')
    assert result.replay_semantics == 'producer_parameters_pinned'
    by_seat = {seat.seat_id: seat for seat in result.per_seat}
    assert by_seat['seat-1'].sti == pytest.approx(0.72)
    assert by_seat['seat-2'].sti == pytest.approx(0.45)


def test_import_marks_missing_and_blocked_seats() -> None:
    spec = _spec(seat_ids=('seat-1', 'seat-2', 'seat-3'))
    result = import_sti_producer_report(
        spec, per_seat_values={'seat-1': 0.72, 'seat-2': None}
    )
    by_seat = {seat.seat_id: seat for seat in result.per_seat}
    assert by_seat['seat-1'].state == 'reported'
    assert by_seat['seat-2'].state == 'blocked'
    assert by_seat['seat-3'].state == 'unavailable'
    assert by_seat['seat-3'].sti is None


def test_unrecoverable_producer_params_mark_replay_dependent() -> None:
    producer = StiProducerRef(
        producer='rew',
        producer_version='5.31.3',
        producer_build=None,
        method='iec_60268_16_2020',
        report_artifact_id='rew-report:2',
        report_sha256=_hash('rew-report-2'),
        producer_parameters_recoverable=False,
    )
    spec = _spec(producer=producer)
    result = import_sti_producer_report(
        spec, per_seat_values={'seat-1': 0.6, 'seat-2': 0.55}
    )
    assert result.replay_semantics == 'producer_dependent'


def test_sti_never_computed_internally() -> None:
    # There is no function that derives STI from an FR magnitude, C50 or
    # RT60 — the only producer path is a qualified imported report.
    import htdt.cad_speech_intelligibility as module

    public = {
        name
        for name in dir(module)
        if not name.startswith('_')
    }
    assert 'compute_sti' not in public
    assert 'sti_from_frequency_response' not in public
