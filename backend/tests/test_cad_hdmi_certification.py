"""#1078 — HDMI cable certification evidence tests."""

import pytest

from htdt.cad_cable_run import (
    CableRunEndpoint,
    CableRunSegment,
    build_cable_run,
)
from htdt.cad_hdmi_certification import (
    bind_certification,
    build_certification_record,
)


def _run(length_m: float):
    return build_cable_run(
        document_id='doc',
        scene_revision_id='rev1',
        scene_content_hash='0' * 64,
        label='AVR->PJ',
        kind='video',
        from_endpoint=CableRunEndpoint(label='avr', entity_id='e1'),
        to_endpoint=CableRunEndpoint(label='pj', entity_id='e2'),
        segments=(
            CableRunSegment(
                sequence=0, path_kind='in_wall', length_m=length_m
            ),
        ),
        created_at_utc='2026-01-01T00:00:00Z',
        run_id='run-1',
    )


def _cert(**kw):
    kw.setdefault('program', 'ultra_high_speed')
    kw.setdefault('qr_payload', 'https://verify.hdmi.org/xyz123')
    kw.setdefault('certified_lengths_m', (2.0,))
    return build_certification_record(
        record_id='cert-1',
        brand='Acme',
        model='UHS-2m',
        verification_uri='https://verify.hdmi.org/',
        **kw,
    )


class TestCertificationRecord:
    def test_qr_hash_verbatim(self):
        c = _cert()
        assert c.qr_payload == 'https://verify.hdmi.org/xyz123'
        assert len(c.qr_payload_sha256) == 64

    def test_wrong_qr_hash_rejected(self):
        from htdt.cad_hdmi_certification import CableCertificationRecord

        with pytest.raises(ValueError, match='qr_payload_sha256'):
            CableCertificationRecord(
                record_id='c2',
                program='premium_high_speed',
                brand='B',
                model='M',
                qr_payload='payload',
                qr_payload_sha256='0' * 64,
                certified_lengths_m=(),
            )


class TestBinding:
    def test_compatible(self):
        b = bind_certification(
            _cert(),
            _run(2.0),
            required_program='ultra_high_speed',
            binding_id='b1',
        )
        assert b.verdict == 'compatible'
        assert b.field_reliability == 'not_evaluated'
        assert b.run_id == 'run-1'

    def test_length_uncovered(self):
        b = bind_certification(
            _cert(),
            _run(3.0),
            required_program='ultra_high_speed',
            binding_id='b2',
        )
        assert b.verdict == 'length_uncovered'

    def test_program_insufficient(self):
        b = bind_certification(
            _cert(program='high_speed'),
            _run(1.0),
            required_program='ultra_high_speed',
            binding_id='b3',
        )
        assert b.verdict == 'program_insufficient'

    def test_unparseable_payload(self):
        b = bind_certification(
            _cert(qr_payload='not a valid token'),
            _run(1.0),
            required_program='ultra_high_speed',
            binding_id='b4',
        )
        assert b.verdict == 'unverifiable_payload'

    def test_no_certified_lengths_allows(self):
        b = bind_certification(
            _cert(certified_lengths_m=()),
            _run(25.0),
            required_program='ultra_high_speed',
            binding_id='b5',
        )
        assert b.verdict == 'compatible'

    def test_certification_never_sets_field_reliability(self):
        b = bind_certification(
            _cert(),
            _run(1.0),
            required_program='ultra_high_speed',
            binding_id='b6',
        )
        assert b.field_reliability == 'not_evaluated'
