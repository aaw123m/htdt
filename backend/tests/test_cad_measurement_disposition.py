"""Append-only measurement disposition/correction authority — round 2.

Measurements are immutable evidence: lifecycle changes are appended as
sealed ``CadMeasurementDisposition`` events and corrected bindings as
``CadMeasurementCorrection`` records. These tests pin the seal hashes, the
lifecycle rules, and the conditional identity payload that keeps pre-#863
rows hash-stable.
"""

from __future__ import annotations

import pytest

from htdt.cad_measurement_disposition import (
    MEASUREMENT_ELIGIBLE_DISPOSITIONS,
    CadMeasurementCorrection,
    CadMeasurementDisposition,
    build_measurement_correction,
    build_measurement_disposition,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _pose_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='htdt.cad-measurement-pose-observation',
        authority_version='1',
        semantic_hash_sha256='e' * 64,
    )


def _correction_kwargs(**overrides):
    payload = dict(
        document_id='doc-1',
        measurement_id='meas-1',
        dataset_id='ds-1',
        dataset_sha256='f' * 64,
        reason='retarget to right seat',
        channel_role='FR',
        correction_id='corr-1',
        created_at_utc='2026-09-26T00:00:00Z',
    )
    payload.update(overrides)
    return payload


def _disposition_kwargs(**overrides):
    payload = dict(
        document_id='doc-1',
        measurement_id='meas-1',
        disposition='active',
        reason='initial import',
        disposition_id='disp-1',
        created_at_utc='2026-09-26T00:00:00Z',
    )
    payload.update(overrides)
    return payload


# --- CadMeasurementDisposition ------------------------------------------------

def test_disposition_builder_seals_deterministically() -> None:
    a = build_measurement_disposition(**_disposition_kwargs())
    b = build_measurement_disposition(**_disposition_kwargs())
    assert a.disposition_sha256 == b.disposition_sha256
    assert len(a.disposition_sha256) == 64
    # Different reason → different seal.
    c = build_measurement_disposition(**_disposition_kwargs(reason='other'))
    assert c.disposition_sha256 != a.disposition_sha256


def test_disposition_corrected_requires_correction_pin() -> None:
    with pytest.raises(ValueError, match='pin the correction'):
        build_measurement_disposition(
            **_disposition_kwargs(disposition='corrected')
        )
    ok = build_measurement_disposition(
        **_disposition_kwargs(disposition='corrected', correction_id='corr-1')
    )
    assert ok.disposition == 'corrected'


def test_disposition_rejects_correction_id_on_non_corrected() -> None:
    with pytest.raises(ValueError, match='only valid for corrected'):
        build_measurement_disposition(
            **_disposition_kwargs(disposition='excluded_from_normal_use', correction_id='corr-1')
        )


def test_disposition_rejects_tampered_hash() -> None:
    payload = build_measurement_disposition(**_disposition_kwargs()).model_dump(mode='python')
    payload['reason'] = 'rewritten'
    with pytest.raises(ValueError, match='hash mismatch'):
        CadMeasurementDisposition(**payload)


# --- CadMeasurementCorrection --------------------------------------------------

def test_correction_requires_at_least_one_changed_field() -> None:
    with pytest.raises(ValueError, match='at least one assignment field'):
        build_measurement_correction(
            **_correction_kwargs(channel_role=None)
        )


def test_correction_pose_evidence_kind_requires_ref() -> None:
    with pytest.raises(ValueError, match='pose_evidence_ref'):
        build_measurement_correction(
            **_correction_kwargs(
                measurement_entity_id='point-right',
                correction_kind='assignment_with_pose_evidence',
            )
        )
    ok = build_measurement_correction(
        **_correction_kwargs(
            measurement_entity_id='point-right',
            correction_kind='assignment_with_pose_evidence',
            pose_evidence_ref=_pose_ref(),
        )
    )
    assert ok.correction_kind == 'assignment_with_pose_evidence'


def test_correction_channel_routing_only_cannot_retarget_entity() -> None:
    with pytest.raises(ValueError, match='cannot replace the target'):
        build_measurement_correction(
            **_correction_kwargs(
                measurement_entity_id='point-right',
                correction_kind='channel_routing_only',
            )
        )
    # Channel-only correction is fine.
    ok = build_measurement_correction(
        **_correction_kwargs(correction_kind='channel_routing_only')
    )
    assert ok.measurement_entity_id is None


def test_correction_builder_seals_and_round_trips() -> None:
    a = build_measurement_correction(**_correction_kwargs())
    assert len(a.correction_sha256) == 64
    payload = a.model_dump(mode='python')
    payload['dataset_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='hash mismatch'):
        CadMeasurementCorrection(**payload)


def test_correction_identity_excludes_optional_fields_when_absent() -> None:
    """Pre-#863 rows keep their hashes: optional kind/ref fields join the
    identity payload only when present."""
    plain = build_measurement_correction(**_correction_kwargs())
    identity = plain.identity_payload()
    assert 'correction_kind' not in identity
    assert 'pose_evidence_ref' not in identity

    with_kind = build_measurement_correction(
        **_correction_kwargs(correction_kind='channel_routing_only')
    )
    assert with_kind.identity_payload()['correction_kind'] == 'channel_routing_only'
    # Adding the optional metadata changes the seal — the record is a new
    # authority statement, not a silent mutation of the old one.
    assert with_kind.correction_sha256 != plain.correction_sha256


def test_correction_source_speaker_ids_replace_wholesale() -> None:
    correction = build_measurement_correction(
        **_correction_kwargs(source_speaker_ids=('sp-a', 'sp-b'))
    )
    assert correction.source_speaker_ids == ('sp-a', 'sp-b')
    assert correction.identity_payload()['source_speaker_ids'] == ['sp-a', 'sp-b']


def test_correction_rejects_bad_dataset_hash_shape() -> None:
    with pytest.raises(ValueError):
        build_measurement_correction(
            **_correction_kwargs(dataset_sha256='not-a-sha')
        )


def test_eligible_dispositions_are_exactly_active_and_corrected() -> None:
    # The eligibility set is the contract every normal-use consumer relies
    # on; any widening must be deliberate.
    assert MEASUREMENT_ELIGIBLE_DISPOSITIONS == frozenset({'active', 'corrected'})


def test_builder_defaults_generate_ids_and_timestamps() -> None:
    kwargs = _correction_kwargs()
    del kwargs['correction_id']
    del kwargs['created_at_utc']
    correction = build_measurement_correction(**kwargs)
    assert correction.correction_id  # uuid4 assigned
    assert correction.created_at_utc
    again = build_measurement_correction(**kwargs)
    assert again.correction_id != correction.correction_id
    assert again.correction_sha256 != correction.correction_sha256
