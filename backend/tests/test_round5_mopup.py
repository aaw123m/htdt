"""Round-5 mop-up regressions: bounded reads, importer validation, and the
acoustics speaker_id KeyError nit."""
from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustics import analyze_rectangular_context
from htdt.cad_benchmark import (
    CANONICAL_JSON_FORMAT,
    BenchmarkSourceAsset,
    canonical_json_importer,
    _hash,
)
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_repository import SceneRepository
from htdt.capture_receiver import (
    CaptureReceiverError,
    CaptureReceiverService,
    TLS_CREDENTIAL_MAX_BYTES,
)
from htdt.ingress import IngressTooLargeError
from htdt.managed_assets import ManagedAssetStore
from scripts import golden_path_preflight


def _provenance(suffix: str) -> EquipmentDataProvenance:
    digit = f'{suffix}0'[-1]
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='bench-suite',
        source_version='1',
        source_reference=f'ref-{suffix}',
        source_sha256=digit * 64,
    )


def _asset(**overrides) -> BenchmarkSourceAsset:
    data = {
        'asset_id': 'asset-1',
        'dataset_name': 'demo-bench',
        'dataset_version': '2026.1',
        'origin_uri': 'https://example.test/bench/demo',
        'content_sha256': 'a' * 64,
        'license_id': 'CC-BY-4.0',
        'admission_ref': 'ledger/demo-bench',
        'evidence_class': 'external_measured',
        'provenance': (_provenance('b1'),),
        'semantic_sha256': '0' * 64,
        **overrides,
    }
    draft = BenchmarkSourceAsset.model_construct(**data)
    data['semantic_sha256'] = _hash(draft.semantic_payload())
    return BenchmarkSourceAsset.model_validate(data)


def _observable() -> dict:
    return {
        'observable_id': 'obs',
        'kind': 'arrival_timing',
        'metric_id': 'abs-delta',
        'metric_version': '1',
        'reference': {'arrival_s': 0.005},
    }


def _payload(**overrides) -> dict:
    base = {
        'format': CANONICAL_JSON_FORMAT,
        'benchmark_id': 'b',
        'version': '1',
        'coordinate_convention': 'cartesian-x-right-y-forward-z-up-m',
        'receivers': [{'point_id': 'rx', 'position_m': [0.0, 0.0, 0.0]}],
        'observables': [_observable()],
    }
    base.update(overrides)
    return base


class TestParsePointValidation:
    def test_malformed_orientation_rejected(self):
        payload = _payload(
            receivers=[
                {
                    'point_id': 'rx',
                    'position_m': [0.0, 0.0, 0.0],
                    'orientation_deg': 'north',
                }
            ]
        )
        with pytest.raises(ValueError, match='malformed orientation_deg'):
            canonical_json_importer(payload, _asset())

    def test_wrong_length_orientation_rejected(self):
        payload = _payload(
            receivers=[
                {
                    'point_id': 'rx',
                    'position_m': [0.0, 0.0, 0.0],
                    'orientation_deg': [0.0, 90.0],
                }
            ]
        )
        with pytest.raises(ValueError, match='malformed orientation_deg'):
            canonical_json_importer(payload, _asset())

    def test_non_finite_position_rejected(self):
        payload = _payload(
            receivers=[
                {'point_id': 'rx', 'position_m': [0.0, float('nan'), 0.0]}
            ]
        )
        with pytest.raises(ValueError, match='non-finite position_m'):
            canonical_json_importer(payload, _asset())

    def test_non_finite_orientation_rejected(self):
        payload = _payload(
            receivers=[
                {
                    'point_id': 'rx',
                    'position_m': [0.0, 0.0, 0.0],
                    'orientation_deg': [0.0, float('inf'), 0.0],
                }
            ]
        )
        with pytest.raises(ValueError, match='non-finite orientation_deg'):
            canonical_json_importer(payload, _asset())

    def test_valid_point_with_orientation_imports(self):
        payload = _payload(
            receivers=[
                {
                    'point_id': 'rx',
                    'position_m': [0.0, 0.0, 0.0],
                    'orientation_deg': [0.0, 90.0, 180.0],
                }
            ]
        )
        case = canonical_json_importer(payload, _asset())
        assert case.receivers[0].orientation_deg == (0.0, 90.0, 180.0)


class TestSpeakerIdFallback:
    def test_speaker_without_id_or_role_does_not_raise(self):
        result = analyze_rectangular_context(
            {
                'room': {
                    'width_m': 4.0,
                    'depth_m': 5.0,
                    'height_m': 2.4,
                    'geometry_kind': 'rectangular',
                },
                'measurement_point': {
                    'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}
                },
                'speakers': [
                    {'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}}
                ],
            },
            max_hz=100.0,
        )
        assert len(result['first_order_reflections']) == 6
        assert all(
            reflection['speaker_id'] == 'unknown'
            for reflection in result['first_order_reflections']
        )


class TestManagedAssetBoundedReads:
    def test_oversized_asset_rejected(self, tmp_path: Path, monkeypatch):
        import htdt.managed_assets as managed_assets

        monkeypatch.setattr(managed_assets, 'MAX_ATTACHMENT_BYTES', 8)
        store = ManagedAssetStore(tmp_path / 'assets')
        target = store.asset_path('ab' * 32)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'x' * 64)
        with pytest.raises(IngressTooLargeError, match='managed asset'):
            ManagedAssetStore.read_file(target)

    def test_within_bound_reads(self, tmp_path: Path, monkeypatch):
        import htdt.managed_assets as managed_assets

        monkeypatch.setattr(managed_assets, 'MAX_ATTACHMENT_BYTES', 8)
        store = ManagedAssetStore(tmp_path / 'assets')
        target = store.asset_path('cd' * 32)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'ok')
        assert ManagedAssetStore.read_file(target) == b'ok'


class TestReceiverTlsBoundedRead:
    @pytest.mark.parametrize(
        ('credential', 'label'),
        [('receiver-cert.pem', 'TLS certificate'), ('receiver-key.pem', 'TLS private key')],
    )
    def test_oversized_pair_rejected_before_ssl_or_regeneration(
        self, tmp_path: Path, monkeypatch, credential: str, label: str
    ):
        import htdt.capture_receiver as receiver

        scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
        service = CaptureReceiverService(scene_repository, data_dir=tmp_path / 'receiver')
        cert_path = service._data_dir / 'receiver-cert.pem'
        key_path = service._data_dir / 'receiver-key.pem'
        cert_path.write_bytes(b'c')
        key_path.write_bytes(b'k')
        oversized = service._data_dir / credential
        oversized.write_bytes(b'x' * (TLS_CREDENTIAL_MAX_BYTES + 1))
        original = (cert_path.read_bytes(), key_path.read_bytes())

        def unexpected(*args, **kwargs):
            pytest.fail('oversized credentials reached SSL parsing or regeneration')

        monkeypatch.setattr(receiver.ssl, 'SSLContext', unexpected)
        monkeypatch.setattr(receiver, 'generate_self_signed_cert', unexpected)
        with pytest.raises(CaptureReceiverError, match=label):
            service._ensure_certificate()
        assert (cert_path.read_bytes(), key_path.read_bytes()) == original

    def test_oversized_certificate_rejected(self, tmp_path: Path):
        scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
        service = CaptureReceiverService(
            scene_repository, data_dir=tmp_path / 'receiver'
        )
        cert_path = service._data_dir / 'receiver-cert.pem'
        key_path = service._data_dir / 'receiver-key.pem'
        cert_path.write_bytes(b'c' * (TLS_CREDENTIAL_MAX_BYTES + 1))
        key_path.write_bytes(b'k')
        with pytest.raises(CaptureReceiverError, match='TLS certificate'):
            service._ensure_certificate()


class TestPreflightCleanFlag:
    def _seed_work_dir(self, work_dir: Path) -> None:
        work_dir.mkdir()
        for artifact in ('data-main', 'data-restored'):
            (work_dir / artifact).mkdir()
            (work_dir / artifact / 'x.txt').write_text('x')
        (work_dir / 'preflight.htdt-backup').write_bytes(b'bak')

    def test_cleanup_removes_artifacts_on_success(
        self, tmp_path: Path, monkeypatch
    ):
        work_dir = tmp_path / 'work'
        self._seed_work_dir(work_dir)
        monkeypatch.setattr(
            golden_path_preflight, 'run_preflight', lambda w, t: 0
        )
        code = golden_path_preflight.main(
            [
                '--work-dir', str(work_dir),
                '--clean-work-dir-on-success',
            ]
        )
        assert code == 0
        assert not (work_dir / 'data-main').exists()
        assert not (work_dir / 'data-restored').exists()
        assert not (work_dir / 'preflight.htdt-backup').exists()
        assert (work_dir / 'golden-path-trace.json').is_file()

    def test_failure_preserves_everything(
        self, tmp_path: Path, monkeypatch
    ):
        work_dir = tmp_path / 'work'
        self._seed_work_dir(work_dir)
        monkeypatch.setattr(
            golden_path_preflight, 'run_preflight', lambda w, t: 1
        )
        code = golden_path_preflight.main(
            [
                '--work-dir', str(work_dir),
                '--clean-work-dir-on-success',
            ]
        )
        assert code == 1
        assert (work_dir / 'data-main').is_dir()

    def test_default_keeps_artifacts_on_success(
        self, tmp_path: Path, monkeypatch
    ):
        work_dir = tmp_path / 'work'
        self._seed_work_dir(work_dir)
        monkeypatch.setattr(
            golden_path_preflight, 'run_preflight', lambda w, t: 0
        )
        code = golden_path_preflight.main(['--work-dir', str(work_dir)])
        assert code == 0
        assert (work_dir / 'data-main').is_dir()
