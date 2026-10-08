# -*- coding: utf-8 -*-
"""#952: rights-safe CLF .CF1/.CF2 handling — licensed-fixture registry,
declared-surface inspection, and the fail-closed compatibility verdict.

Two clearly separated fixture lanes:

- ``TestRealManufacturerRecords`` / ``TestLicensedFixtureRegistry`` —
  pin the REAL manufacturer-distributed .CF1/.CF2 files located by the
  rights investigation (SHA256 + size + verbatim permission context).
  No real bytes are vendored: no source grants redistribution, so the
  registry is hash-and-license evidence only.
- everything else — SYNTHETIC signature stubs and synthetic text; the
  evaluator mechanics are exercised on bytes we fully control.
"""

from __future__ import annotations

import hashlib
import os
import re

from htdt.cad_loudspeaker_interchange import (
    CLF_FIELD_DECLARATIONS,
    CLF_LICENSED_FIXTURES,
    ClfFixtureRecord,
    clf_fixture_for_sha256,
    detect_clf_binary_variant,
    detect_clf_family,
    evaluate_clf_payload,
    inspect_clf_declared_surface,
    qualify_clf,
    verify_clf_fixture,
)
from htdt.interop_corpus_fixtures import (
    _clf_binary_cf1,
    _clf_binary_cf2_v2,
)


# --- synthetic helpers -------------------------------------------------------

def _cf(lead: int, generation: int, version_digit: bytes) -> bytes:
    """Signature stub: lead + BD 0A 00 + generation, ``v{d}.0`` at 20."""
    head = bytes([lead, 0xBD, 0x0A, 0x00, generation])
    gap = bytes(15)
    marker = b'v' + version_digit + b'.0'
    tail = bytes(64)
    return head + gap + marker + tail


def _record_for_bytes(payload: bytes, **overrides) -> ClfFixtureRecord:
    """A fixture record pinned to arbitrary (synthetic) bytes — lets
    tests exercise the real-fixture evaluator path deterministically."""
    kwargs = dict(
        fixture_id='clfx-synthetic-test',
        file_name='synthetic.CF2',
        family='binary_cf2',
        binary_variant='cf2_v1',
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        manufacturer='Synthetic Co',
        model='TEST-1',
        source_kind='manufacturer_direct',
        source_url='https://example.invalid/x.CF2',
        source_page='https://example.invalid/',
        licence_verbatim='Synthesized license text for tests.',
        use_grant='granted',
        analysis_grant='granted',
        redistribution_grant='not_stated',
        import_route='binary_decode',
        declared_quantities=('balloon_spl_magnitude_1third_oct_5deg',),
    )
    kwargs.update(overrides)
    return ClfFixtureRecord(**kwargs)


LICENSED_TEXT = (
    b'[HEADER]\nManufacturer: Acme\nLicense: CC-BY-4.0\n'
    b'[FREQUENCY]\n100 90.0 0.0\n200 91.0 0.5\n[POLAR]\n0 0\n'
)
UNLICENSED_TEXT = (
    b'[HEADER]\nManufacturer: Acme\n'
    b'[FREQUENCY]\n100 90.0 0.0\n[POLAR]\n0 0\n'
)
UNQUALIFIED_TEXT = (
    b'[HEADER]\nManufacturer: Acme\nLicense: CC-BY-4.0\n'
    b'[FREQUENCY]\n# only comments, no numeric rows\n'
)
UNQUALIFIED_UNLICENSED_TEXT = (
    b'[HEADER]\nManufacturer: Acme\n[FREQUENCY]\n# nothing\n'
)


# --- signature hardening (synthetic) ----------------------------------------

class TestSignatureHardening:
    def test_gen2_cf2_signature_detected(self) -> None:
        # The CLF viewer kit's real clf2_v2_* samples carry this exact
        # signature (41 BD 0A 00 02 + v2.0) — previously misreported
        # as not_clf.
        stub = _clf_binary_cf2_v2()
        assert detect_clf_family(stub) == 'binary_cf2'
        assert detect_clf_binary_variant(stub) == 'cf2_v2'
        q = qualify_clf(stub)
        assert q.verdict == 'unsupported'
        assert q.binary_variant == 'cf2_v2'

    def test_gen1_signatures_unchanged(self) -> None:
        assert detect_clf_binary_variant(_clf_binary_cf1()) == 'cf1_v1'
        assert detect_clf_binary_variant(_cf(0x41, 0x01, b'1')) == 'cf2_v1'

    def test_cf1_gen2_is_not_a_real_layout(self) -> None:
        # .CF1 only exists in the v1 layout — a gen-2 0x40 lead fails
        # closed instead of inventing a variant.
        assert detect_clf_family(_cf(0x40, 0x02, b'2')) == 'unknown'

    def test_generation_marker_mismatch_fails_closed(self) -> None:
        # No real file ever mixes 01+v2.0 or 02+v1.0 (all 14 kit
        # samples agree); forged signatures stay unknown.
        assert detect_clf_family(_cf(0x41, 0x01, b'2')) == 'unknown'
        assert detect_clf_family(_cf(0x41, 0x02, b'1')) == 'unknown'

    def test_truncated_signature_fails_closed(self) -> None:
        assert detect_clf_family(_cf(0x41, 0x02, b'2')[:19]) == 'unknown'
        assert detect_clf_binary_variant(b'\x41\xbd\x0a') == ''


# --- real-manufacturer fixture registry (pinned records, no vendored bytes) --

class TestLicensedFixtureRegistry:
    """The registry records are the rights evidence — each pins a real
    manufacturer file by SHA256/size with its verbatim permission text.
    """

    def test_at_least_one_cf1_and_one_cf2_manufacturer_fixture(self) -> None:
        manufacturers = {
            r.family for r in CLF_LICENSED_FIXTURES
            if r.source_kind in (
                'clf_group_distribution', 'manufacturer_direct',
            )
        }
        assert manufacturers == {'binary_cf1', 'binary_cf2'}

    def test_every_record_has_verbatim_permission_context(self) -> None:
        for record in CLF_LICENSED_FIXTURES:
            assert record.licence_verbatim
            assert record.use_grant in (
                'granted', 'not_stated', 'not_granted',
            )
            assert record.analysis_grant in (
                'granted', 'not_stated', 'not_granted',
            )
            assert re.fullmatch(r'[0-9a-f]{64}', record.content_sha256)
            assert record.fixture_id.startswith('clfx-')

    def test_family_variant_consistency(self) -> None:
        for record in CLF_LICENSED_FIXTURES:
            if record.family == 'binary_cf1':
                assert record.binary_variant == 'cf1_v1'
            else:
                assert record.binary_variant in ('cf2_v1', 'cf2_v2')

    def test_no_record_grants_redistribution(self) -> None:
        # Honest outcome of the investigation: no source states
        # redistribution permission, so no fixture bytes are vendored —
        # evidence is hash + license text only.
        for record in CLF_LICENSED_FIXTURES:
            assert record.redistribution_grant in (
                'not_stated', 'not_granted',
            )

    def test_v2_generation_fixture_is_pinned(self) -> None:
        assert any(
            r.binary_variant == 'cf2_v2' for r in CLF_LICENSED_FIXTURES
        )

    def test_declared_quantities_never_inferred(self) -> None:
        for record in CLF_LICENSED_FIXTURES:
            assert record.declared_quantities


# --- declared-surface inspection --------------------------------------------

class TestDeclaredSurface:
    def test_bounded_printable_extraction(self) -> None:
        payload = (
            _cf(0x41, 0x01, b'1')
            + b'ACME Loudspeaker Co\x00\x01MODEL-99'
            + bytes(4096)
        )
        surface = inspect_clf_declared_surface(payload)
        assert surface.family == 'binary_cf2'
        assert surface.binary_variant == 'cf2_v1'
        joined = ' '.join(surface.declared_strings)
        assert 'ACME Loudspeaker Co' in joined
        assert 'MODEL-99' in joined

    def test_surface_never_reads_past_bound(self) -> None:
        # A string placed beyond the 4 KiB declared surface is NOT
        # extracted — the inspector must stay inside the documented
        # header region.
        payload = _cf(0x41, 0x01, b'1') + bytes(4096) + b'HIDDEN-STRING'
        surface = inspect_clf_declared_surface(payload)
        assert not any('HIDDEN' in s for s in surface.declared_strings)

    def test_text_payload_has_empty_surface(self) -> None:
        surface = inspect_clf_declared_surface(LICENSED_TEXT)
        assert surface.family == 'authoring_text'
        assert surface.declared_strings == ()

    def test_verify_fixture_matches_pinned_bytes(self) -> None:
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload)
        assert verify_clf_fixture(record, payload) == 'matches'

    def test_verify_fixture_rejects_tampered_bytes(self) -> None:
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload)
        tampered = bytearray(payload)
        tampered[40] ^= 0xFF  # same signature, corrupted payload
        assert verify_clf_fixture(record, bytes(tampered)) == 'mismatch'
        # record pinned to the payload but claiming the wrong variant
        record_v2 = _record_for_bytes(
            payload, binary_variant='cf2_v2',
        )
        assert verify_clf_fixture(record_v2, payload) == 'mismatch'


# --- compatibility verdict: synthetic payloads through the evaluator ---------

class TestEvaluatePayloadSynthetic:
    def test_unlicensed_binary_rights_unknown(self) -> None:
        ev = evaluate_clf_payload(_cf(0x41, 0x02, b'2'))
        assert ev.outcome == 'rights_unknown'
        assert ev.binary_variant == 'cf2_v2'
        assert ev.columns.available == 'PASS'
        assert ev.columns.rights_admissible == 'UNKNOWN'
        assert ev.columns.schema_admitted == 'BLOCKED'

    def test_not_clf_payload(self) -> None:
        ev = evaluate_clf_payload(os.urandom(256))
        assert ev.outcome == 'not_clf'
        assert ev.columns.available == 'BLOCKED'

    def test_licensed_fixture_binary_is_decode_blocked(self) -> None:
        # Reproduces the REAL manufacturer path deterministically: a
        # licensed record pinned to supplied bytes still resolves to
        # decode_blocked because no licensed decoder exists.
        payload = _cf(0x41, 0x01, b'1') + bytes(300)
        record = _record_for_bytes(payload)
        ev = evaluate_clf_payload(payload, fixture=record)
        assert ev.outcome == 'decode_blocked'
        assert ev.fixture_match == 'matches'
        assert ev.columns.rights_admissible == 'PARTIAL'
        assert ev.columns.schema_admitted == 'UNSUPPORTED'
        assert ev.columns.solver_input_sufficient == 'UNKNOWN'
        assert ev.columns.reproducibly_predicted == 'BLOCKED'

    def test_fixture_denying_use_is_no_licensed_fixture(self) -> None:
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload, use_grant='not_granted')
        ev = evaluate_clf_payload(payload, fixture=record)
        assert ev.outcome == 'no_licensed_fixture'
        assert ev.columns.rights_admissible == 'BLOCKED'

    def test_record_with_not_stated_grants_is_rights_unknown(self) -> None:
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload, analysis_grant='not_stated')
        ev = evaluate_clf_payload(payload, fixture=record)
        assert ev.outcome == 'rights_unknown'

    def test_pin_mismatch_is_tamper_evidence_not_the_fixture(self) -> None:
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload)
        drifted = payload + b'\x00'  # same signature, different sha
        ev = evaluate_clf_payload(drifted, fixture=record)
        assert ev.outcome == 'rights_unknown'
        assert ev.fixture_match == 'mismatch'
        assert ev.fixture_id == record.fixture_id

    def test_evaluator_never_decodes_declared_only(self) -> None:
        # Even for a licensed fixture the data arrays stay sealed: the
        # evaluation carries declarations, never a normalized dataset.
        payload = _cf(0x41, 0x01, b'1')
        record = _record_for_bytes(payload)
        ev = evaluate_clf_payload(payload, fixture=record)
        assert ev.columns.schema_admitted == 'UNSUPPORTED'
        assert 'balloon' in ' '.join(ev.columns.reasons)


class TestEvaluatePayloadAuthoringText:
    def test_licensed_qualified_text_is_licensed_ok(self) -> None:
        ev = evaluate_clf_payload(LICENSED_TEXT)
        assert ev.outcome == 'licensed_ok'
        assert ev.columns.rights_admissible == 'PASS'
        assert ev.columns.schema_admitted == 'PASS'
        assert ev.columns.solver_input_sufficient == 'PARTIAL'

    def test_qualified_text_without_license_is_rights_unknown(self) -> None:
        ev = evaluate_clf_payload(UNLICENSED_TEXT)
        assert ev.outcome == 'rights_unknown'
        assert ev.columns.rights_admissible == 'UNKNOWN'

    def test_licensed_unqualified_text_is_solver_input_insufficient(
        self,
    ) -> None:
        ev = evaluate_clf_payload(UNQUALIFIED_TEXT)
        assert ev.outcome == 'solver_input_insufficient'
        assert ev.columns.schema_admitted == 'BLOCKED'
        assert ev.columns.solver_input_sufficient == 'BLOCKED'

    def test_unqualified_unlicensed_text_is_rights_unknown(self) -> None:
        ev = evaluate_clf_payload(UNQUALIFIED_UNLICENSED_TEXT)
        assert ev.outcome == 'rights_unknown'


# --- field-level honesty -----------------------------------------------------

class TestFieldDeclarations:
    def test_every_field_has_documented_definition(self) -> None:
        assert len(CLF_FIELD_DECLARATIONS) >= 8
        for decl in CLF_FIELD_DECLARATIONS:
            assert decl.field
            assert decl.clf_definition
            assert decl.per_file_state != 'not_decoded' or (
                decl.htdt_state == 'documented_spec_only'
            )

    def test_required_fields_covered(self) -> None:
        fields = {d.field for d in CLF_FIELD_DECLARATIONS}
        assert {
            'coordinate_convention',
            'angular_resolution',
            'units',
            'reference_distance',
            'absolute_spl_vs_sensitivity',
            'phase_presence',
        } <= fields

    def test_only_authorship_surface_is_readable(self) -> None:
        readable = {
            d.field for d in CLF_FIELD_DECLARATIONS
            if decl_state(d) == 'declared_surface_readable'
        }
        assert readable == {'origin_authorship'}


def decl_state(decl) -> str:
    return decl.htdt_state


# --- real records resolve through the evaluator (record-level) ---------------

class TestRealManufacturerRecords:
    """Record-level honesty for the real manufacturer pins: rights state,
    outcome mapping, and field declarations — without touching payload
    bytes that may not be redistributed."""

    def test_real_records_resolve_as_licensed(self) -> None:
        from htdt.cad_loudspeaker_interchange import _rights_state

        for record in CLF_LICENSED_FIXTURES:
            assert _rights_state(record) == 'licensed'

    def test_lookup_by_sha256(self) -> None:
        record = CLF_LICENSED_FIXTURES[0]
        assert clf_fixture_for_sha256(record.content_sha256) is record
        assert clf_fixture_for_sha256('0' * 64) is None

    def test_record_sources_are_public_manufacturer_channels(self) -> None:
        for record in CLF_LICENSED_FIXTURES:
            assert record.source_url.startswith('http')
            assert record.source_page.startswith('http')
            assert record.source_kind in (
                'clf_group_distribution',
                'manufacturer_direct',
                'clf_group_sample_kit',
            )
