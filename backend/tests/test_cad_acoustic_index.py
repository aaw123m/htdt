"""#1059 — Acoustic Index connector tests (read-through, offline-safe)."""

import json

import pytest

from htdt.cad_acoustic_index import (
    AcousticIndexConnector,
    AcousticIndexImportError,
    AcousticIndexOfflineError,
    ISO354_OCTAVE_BANDS_HZ,
    snapshot_from_compare_response,
    validate_snapshot_for_import,
)


def _bands(values=(0.2, 0.4, 0.6)):
    return {str(f): v for f, v in zip((100.0, 1000.0, 4000.0), values)}


def _full_octave(value=0.5):
    return {str(f): value for f in ISO354_OCTAVE_BANDS_HZ}


def _product(**over):
    """A product payload shaped like a real compare-products response."""
    product = {
        'id': 42,
        'label': 'Example Panel',
        'commercial_name': 'Panel Pro',
        'manufacturerName': 'Acme Acoustics',
        'material_category': 'porous',
        'data_source': 'ptb',
        'alphaW': 0.65,
        'alphaWMin': 0.6,
        'installation_types': ['A'],
        'structured_meta': {'thickness': '50mm', 'backing': 'solid'},
        'absorption_iso354': [
            {
                'id': 7,
                'measuringOption': 'measured',
                'sourceKind': 'lab_report',
                'notes': 'ISO 354 room method',
                'variantMeta': {'thickness': '50mm', 'mounting': 'A'},
                'alphaSOct': _full_octave(),
                'alphaSTerz': _bands(),
                'alphaPOct': _bands(),
                'derived': {
                    'nrc': 0.6,
                    'nrcBasis': 'astm_c423',
                    'formulaVersion': '1',
                    'sourceStandard': 'ISO 11654',
                },
            },
        ],
    }
    product.update(over)
    return product


class TestSnapshot:
    def test_snapshot_pins_response_hash(self):
        snap = snapshot_from_compare_response(
            product=_product(), fetched_utc='2026-09-25T00:00:00Z'
        )
        assert len(snap.response_sha256) == 64
        assert snap.variants[0].buildup_fingerprint()

    def test_metric_families_stay_separate(self):
        snap = snapshot_from_compare_response(
            product=_product(), fetched_utc='2026-09-25T00:00:00Z'
        )
        v = snap.variants[0]
        assert v.alpha_s_oct is not None
        assert v.alpha_s_terz is not None
        assert v.alpha_p_oct is not None
        assert v.derived.nrc == 0.6
        # NRC/SAA is derived and stays on the derived block, never on
        # alpha_w and never recomputed from the bands
        assert snap.alpha_w == 0.65
        assert snap.alpha_w != v.derived.nrc

    def test_evidence_tier_preserved_verbatim(self):
        snap = snapshot_from_compare_response(
            product=_product(), fetched_utc='2026-09-25T00:00:00Z'
        )
        assert snap.data_source == 'ptb'
        assert snap.variants[0].measuring_option == 'measured'
        assert snap.variants[0].source_kind == 'lab_report'


class TestImportValidation:
    def test_clean_snapshot_imports(self):
        snap = snapshot_from_compare_response(
            product=_product(), fetched_utc='2026-09-25T00:00:00Z'
        )
        assert validate_snapshot_for_import(snap) == ()

    def test_no_variants_rejected(self):
        snap = snapshot_from_compare_response(
            product=_product(absorption_iso354=[]),
            fetched_utc='2026-09-25T00:00:00Z',
        )
        with pytest.raises(AcousticIndexImportError):
            validate_snapshot_for_import(snap)

    def test_missing_buildup_identity_rejected(self):
        product = _product()
        product['absorption_iso354'][0]['variantMeta'] = {}
        snap = snapshot_from_compare_response(
            product=product, fetched_utc='2026-09-25T00:00:00Z'
        )
        with pytest.raises(AcousticIndexImportError):
            validate_snapshot_for_import(snap)

    def test_missing_bands_fail_closed(self):
        product = _product()
        # drop the 5000 Hz band — incomplete octave set, no silent fill
        del product['absorption_iso354'][0]['alphaSOct']['5000.0']
        snap = snapshot_from_compare_response(
            product=product, fetched_utc='2026-09-25T00:00:00Z'
        )
        with pytest.raises(AcousticIndexImportError):
            validate_snapshot_for_import(snap)

    def test_out_of_bounds_coefficients_rejected(self):
        product = _product()
        product['absorption_iso354'][0]['alphaSOct']['100.0'] = 3.5
        snap = snapshot_from_compare_response(
            product=product, fetched_utc='2026-09-25T00:00:00Z'
        )
        with pytest.raises(AcousticIndexImportError):
            validate_snapshot_for_import(snap)


class _FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.responses[url]


class TestConnector:
    def test_offline_search_returns_nothing_live(self):
        conn = AcousticIndexConnector(None)
        result = conn.search('wool', retrieved_utc='t')
        assert result.live_fetch is False
        assert result.candidates == ()

    def test_offline_snapshot_requires_cache(self):
        conn = AcousticIndexConnector(None)
        with pytest.raises(AcousticIndexOfflineError):
            conn.snapshot_product('42', fetched_utc='t')

    def test_search_maps_candidates_not_evidence(self):
        from htdt.cad_acoustic_index import (
            ACOUSTIC_INDEX_PUBLIC_SEARCH,
        )
        fetcher = _FakeFetcher({
            f'{ACOUSTIC_INDEX_PUBLIC_SEARCH}?q=panel': {
                'items': [_product()],
            },
        })
        conn = AcousticIndexConnector(fetcher)
        result = conn.search('panel', retrieved_utc='t')
        assert result.live_fetch is True
        assert result.candidates[0].product_id == '42'
        assert 'absorption_iso354' not in (
            type(result.candidates[0]).model_fields
        )

    def test_rebind_on_upstream_change(self):
        from htdt.cad_acoustic_index import (
            ACOUSTIC_INDEX_PUBLIC_COMPARE,
        )
        url = f'{ACOUSTIC_INDEX_PUBLIC_COMPARE}?id=42'
        fetcher = _FakeFetcher({url: [_product()]})
        conn = AcousticIndexConnector(fetcher)
        first = conn.snapshot_product('42', fetched_utc='t1')

        # upstream changes -> a NEW snapshot superseding the old,
        # not a rewrite of the cached object
        fetcher.responses[url] = [_product(alphaW=0.7)]
        second = conn.snapshot_product('42', fetched_utc='t2')
        assert second.snapshot_id != first.snapshot_id
        assert second.supersedes_snapshot_id == first.snapshot_id
        assert first.alpha_w == 0.65  # original untouched

    def test_bounded_cache_evicts_oldest(self):
        from htdt.cad_acoustic_index import (
            ACOUSTIC_INDEX_PUBLIC_COMPARE,
        )
        responses = {
            f'{ACOUSTIC_INDEX_PUBLIC_COMPARE}?id={i}': [
                _product(id=i)
            ]
            for i in range(5)
        }
        conn = AcousticIndexConnector(
            _FakeFetcher(responses), max_entries=2
        )
        for i in range(3):
            conn.snapshot_product(str(i), fetched_utc='t')
        assert conn.cached_snapshot('0') is None  # evicted
        assert conn.cached_snapshot('2') is not None
