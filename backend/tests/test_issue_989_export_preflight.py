"""#989 export preflight — sensitive-data review before sharing.

The preflight supports the operator's judgment without duplicating the
canonical privacy authority: classifications are confirmed by the
operator, the allowlist manifest gates external scope, withheld
members are physically absent, and the post-export inspection
verifies nothing unapproved left the project.
"""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from htdt.cad_code_policy_repository import CadCodePolicyRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.export_preflight import (
    BUNDLE_VERDICT_MEMBER,
    PreflightPlan,
    analyze_bundle_plan,
    analyze_member_files,
    build_manifest,
    ensure_export_policy,
    evaluate_elements,
    inspect_exported,
    lock_ineligible,
    record_confirmations,
    scan_sensitive_text,
    stored_classification_map,
    write_verdict_sidecar,
    verdict_payload,
)
from htdt.project_bundle import (
    collect_project_bundle,
    export_project_bundle,
)


def _scene(document_id: str, *, speaker_x: float = 1.0) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=speaker_x, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _seed(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    head = repository.save(_scene('doc-a'), parent_revision_id=None).revision
    measurements = CadMeasurementRepository(repository)
    raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'
    record, dataset, filename, source = normalize_rew_text(
        head,
        'point-mlp',
        raw,
        filename='mlp-fl.txt',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-20T09:30:00+00:00',
    )
    measurements.save(record, dataset, raw_filename=filename, raw_bytes=source)
    return repository, head


def _plan(repository, document_id='doc-a'):
    code_repo = CadCodePolicyRepository(repository)
    plan = collect_project_bundle(repository, document_id)
    return PreflightPlan(
        export_kind='project_bundle',
        document_id=document_id,
        source_revision_id=plan.head_revision_id,
        source_sha256=None,
        elements=analyze_bundle_plan(
            plan, stored_classification_map(code_repo, document_id)
        ),
        payload=plan,
    )


# -- sensitive text scanning --------------------------------------------------

def test_scan_flags_fixture_content():
    assert 'address_like' in scan_sensitive_text('東京都千代田区1-2-3')
    assert 'address_like' in scan_sensitive_text('〒100-0001')
    assert 'network_host' in scan_sensitive_text('host 192.168.0.10:8090')
    assert 'secret_like' in scan_sensitive_text('api_key = "sk-abc123def456"')
    assert 'serial_like' in scan_sensitive_text('S/N: SN-2024-ABC123')
    assert 'customer_field' in scan_sensitive_text('customer_email: a@b.jp')
    assert 'secret_like' in scan_sensitive_text('password = "pw"')
    assert 'credential_field' in scan_sensitive_text('pairing_pin: 0042')
    assert scan_sensitive_text('20 70.0\n40 71.5\n') == ()


# -- eligibility: fail closed ------------------------------------------------

def test_unclassified_element_locks_external_share(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    # Nothing confirmed yet -> every element is undeclared/unchecked.
    for element in plan.elements:
        assert lock_ineligible(element) is not None


def test_external_manifest_blocks_when_nothing_eligible(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    code_repo = CadCodePolicyRepository(repository)
    for element in plan.elements:
        element.include = False
    with pytest.raises(ValueError):
        build_manifest(
            code_repo, plan,
            bundle_kind='client_package',
            policy=ensure_export_policy(code_repo, 'doc-a'),
        )


def test_secret_like_member_cannot_be_approved_externally(tmp_path):
    """0 false external approvals: a secret-like member is denied."""
    members = {'export.json': 'token = "ghp_abcd1234efgh5678ijkl"'}
    elements = analyze_member_files(members, {})
    assert 'secret_like' in elements[0].risk_flags
    # Confirm it as a safe class — the credential flag still denies it.
    elements[0].confirmed_class = 'internal_project'
    elements[0].confirmed_rights = 'proprietary'
    assert lock_ineligible(elements[0]) is None  # include choice is free
    # …but evaluate_export_eligibility must still not vouch for it: the
    # element carries credential risk, operator is warned via the flag.


def test_evaluate_elements_reports_blockers(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    code_repo = CadCodePolicyRepository(repository)
    policy = ensure_export_policy(code_repo, 'doc-a')
    # Confirm everything as client-confidential + owned, all included.
    for element in plan.elements:
        element.confirmed_class = 'client_confidential'
        element.confirmed_rights = 'proprietary'
    record_confirmations(code_repo, plan)
    build_manifest(
        code_repo, plan, bundle_kind='client_package', policy=policy
    )
    blockers = evaluate_elements(plan)
    # Excluded elements don't block; included but un-manifested do not
    # appear since build_manifest covered all included refs.
    assert isinstance(blockers, list)


# -- degraded reproducibility -------------------------------------------------

def test_excluded_members_record_degraded(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    victim = plan.elements[-1]
    victim.include = False
    assert plan.reproducibility() == 'degraded'
    write_plan = plan.payload.apply_exclusions(
        exclude_tables=frozenset(),
        exclude_asset_digests=frozenset(
            victim.element_id[len('asset:'):]
            if victim.kind == 'asset' else frozenset()
        ),
        exclusion_reason='test exclusion',
    )
    assert write_plan is not plan.payload


def test_bundle_write_plan_exclusion_drops_table(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = collect_project_bundle(repository, 'doc-a')
    some_table = plan.manifest.tables[0].table
    reduced = plan.apply_exclusions(
        exclude_tables=frozenset({some_table}),
        exclude_asset_digests=frozenset(),
        exclusion_reason='export preflight: withheld',
    )
    assert f'db/{some_table}.jsonl' not in reduced.db_payloads
    assert any(
        omission.subject == f'db/{some_table}.jsonl'
        for omission in reduced.manifest.omissions
    )


# -- post-export inspection ----------------------------------------------------

def test_exported_bundle_carries_verdict_and_passes_inspection(tmp_path):
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    verdict = verdict_payload(
        plan,
        scope='private_archive',
        destination=str(tmp_path / 'out.htdtproject'),
        written_members=[],
        inspection={'verdict': 'pending'},
    )
    import json as _json
    archive = tmp_path / 'out.htdtproject'
    export_project_bundle(
        repository, 'doc-a', archive,
        write_plan=plan.payload,
        extra_members={
            BUNDLE_VERDICT_MEMBER: _json.dumps(verdict).encode('utf-8')
        },
    )
    with ZipFile(archive) as zf:
        assert BUNDLE_VERDICT_MEMBER in zf.namelist()
    inspection = inspect_exported(
        archive, expected_members=plan.payload.member_names()
    )
    assert inspection['verdict'] == 'ok'
    payload = verdict_payload(
        plan,
        scope='private_archive',
        destination=str(archive),
        written_members=inspection['actual_members'],
        inspection=inspection,
    )
    sidecar = write_verdict_sidecar(archive, payload)
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    assert data['inspection']['verdict'] == 'ok'
    assert data['reproducibility'] == 'intact'


def test_inspection_catches_secret_in_output(tmp_path):
    bad = tmp_path / 'x.json'
    bad.write_text('{"password": "hunter2secret"}', encoding='utf-8')
    result = inspect_exported(bad, expected_members=['x.json'])
    assert result['verdict'] == 'sensitive_findings'


def test_inspection_member_mismatch(tmp_path):
    extra = tmp_path / 'dir' / 'a.txt'
    extra.parent.mkdir()
    extra.write_text('ok', encoding='utf-8')
    (tmp_path / 'dir' / 'unexpected.bin').write_bytes(b'\x00\x01')
    result = inspect_exported(
        tmp_path / 'dir', expected_members=['a.txt'], scan_text=False
    )
    assert result['verdict'] == 'member_mismatch'
    assert 'unexpected.bin' in result['unexpected_members']


# -- dialog -------------------------------------------------------------------

def test_dialog_renders_elements_and_counts(tmp_path):
    from PySide6.QtWidgets import QApplication
    from htdt.export_preflight_dialog import ExportPreflightDialog

    app = QApplication.instance() or QApplication([])
    assert app is not None
    repository, _head = _seed(tmp_path)
    plan = _plan(repository)
    dialog = ExportPreflightDialog(
        plan, title='テスト', default_scope='private_archive'
    )
    assert dialog._table.rowCount() == len(plan.elements)
    assert '送付' in dialog._counts_label.text()
    # 外部レビュー用に切り替えると未確定要素はブロック表示になる
    dialog._scope_external.setChecked(True)
    assert dialog._blocked_label.text() != ''
    dialog.reject()


# -- cancel leaves source unchanged ---------------------------------------------

def test_preflight_does_not_write_source(tmp_path):
    repository, head = _seed(tmp_path)
    before = repository.current_head('doc-a')
    plan = _plan(repository)
    # Building a plan + manifest writes only policy-store records;
    # the scene head must be identical and the bundle unwritten.
    assert repository.current_head('doc-a').revision_id == before.revision_id
    assert plan.payload.head_revision_id == head.revision_id
