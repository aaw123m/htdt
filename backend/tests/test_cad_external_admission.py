"""Shared external-asset admission record tests (#1060/#1066/#1067/#1068)."""

import pytest

from htdt.cad_external_admission import (
    build_external_asset_admission,
    external_asset_file,
    admission_file,
)


def _file(**kw):
    defaults = dict(
        file_name='payload.zip',
        uri='https://example.org/payload.zip',
        size_bytes=1000,
        md5='a' * 32,
    )
    defaults.update(kw)
    return external_asset_file(**defaults)


def _admission(**kw):
    defaults = dict(
        admission_id='ledger/x',
        dataset_name='x',
        dataset_title='X dataset',
        publisher='pub',
        source_kind='zenodo_record',
        admission_state='download_on_demand_candidate',
        license_id='cc-by-4.0',
        license_family='cc_by',
        record_uri='https://zenodo.org/records/1',
        files=(_file(),),
    )
    defaults.update(kw)
    return build_external_asset_admission(**defaults)


class TestExternalAssetFile:
    def test_semantic_hash_verifies(self):
        f = _file()
        assert len(f.semantic_sha256) == 64

    def test_missing_checksum_requires_none_source(self):
        with pytest.raises(Exception):
            _file(md5=None, sha256=None, checksum_source='publisher')

    def test_none_source_rejects_checksums(self):
        with pytest.raises(Exception):
            _file(checksum_source='none')


class TestExternalAssetAdmission:
    def test_duplicate_file_names_rejected(self):
        with pytest.raises(Exception):
            _admission(files=(_file(), _file()))

    def test_admission_file_lookup(self):
        a = _admission(
            files=(
                _file(file_name='a.zip'),
                _file(file_name='b.zip', md5='b' * 32),
            )
        )
        assert admission_file(a, 'b.zip').md5 == 'b' * 32
        assert admission_file(a, 'missing') is None

    def test_state_vocabulary_ledger_compatible(self):
        # #834 ledger states admit unchanged
        for state in (
            'bundle_candidate', 'download_on_demand_candidate',
            'user_import_candidate', 'license_review_required',
            'third_party_discovery_only',
        ):
            a = _admission(admission_state=state)
            assert a.admission_state == state
