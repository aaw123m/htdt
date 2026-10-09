"""Export preflight — human review before anything leaves the project (#989).

The canonical privacy authority (``cad_project_data_privacy``) already
fails closed for external sharing; what did not exist was the GUI path
where a human reviews per-element classification, send scope and
omit/degrade reasons before export. This module is that review's engine:

* ``analyze_*`` enumerates exactly what an export would carry — tables,
  managed assets or generated member files — with sizes and sensitivity
  risk flags (photos, raw measurements, device serials, network
  endpoints, customer fields, secret-like strings).
* The dialog scope 完全再現用（非公開保存） / 外部レビュー用（最小限の項目）
  decides the gate: private archives ship everything (display only),
  external shares go through ``evaluate_export_eligibility`` against an
  allowlist ``ExportRedactionManifest``.
* Classifications the operator confirms are persisted as sealed
  ``ProjectDataClassification`` records — human judgement becomes
  authority; nothing is auto-sanitized or auto-approved.
* Unclassified / scope-mismatch / unknown credential / unconfirmed
  rights can never be included externally — they become manifest
  ``excluded_refs`` (documented, never silent) or block the export
  entirely when nothing eligible remains.
* After writing, ``inspect_exported`` re-opens the artifact to verify
  the member set matches the approved manifest and no secret-like
  content slipped in; the verdict lands in a sidecar (and inside the
  bundle as ``privacy/preflight-verdict.json``). Nothing is sent
  anywhere — HTDT never transmits on its own.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .cad_authority_resolver import AuthorityRef
from .cad_code_policy_repository import CadCodePolicyRepository
from .cad_project_data_privacy import (
    DataClass,
    ExportRedactionManifest,
    ProjectDataClassification,
    RightsClass,
    SensitiveArtifactPolicy,
    evaluate_export_eligibility,
)
from .canonical_json import canonical_sha256


PreflightScope = Literal['private_archive', 'external_review']

_SCOPE_LABELS: dict[PreflightScope, str] = {
    'private_archive': '完全再現用（非公開保存）',
    'external_review': '外部レビュー用（最小限の項目）',
}

_EXPORT_KIND_LABELS: dict[str, str] = {
    'project_bundle': 'プロジェクトバンドル (.htdtproject)',
    'review_package': 'レビューパッケージ',
    'proposal_package': '提案パッケージ',
    'installation_handoff': '設置ハンドオフ',
    'analysis_export': '解析エクスポート',
}

_CLASS_LABELS: dict[str, str] = {
    'public_shareable': '公開可',
    'internal_project': '社内・プロジェクト内部',
    'client_confidential': '顧客機密',
    'personal_pii': '個人情報',
    'security_sensitive': 'セキュリティ機密',
    'credential_or_secret': '秘密情報（出力不可）',
    'proprietary_license_restricted': 'ライセンス制限',
    'safety_engineering_restricted': '安全工学制限',
    'unknown_classification': '未分類',
}

_RIGHTS_LABELS: dict[str, str] = {
    'open': 'オープン',
    'reference_only': '参照のみ',
    'licensed_no_redistribution': '再配布不可',
    'proprietary': 'プロプライエタリ',
    'undeclared': '権利未確認',
}

_RISK_LABELS: dict[str, str] = {
    'photo': '写真・画像を含む可能性',
    'raw_recording': '生測定・録音データ',
    'raw_mesh': '生スキャン・メッシュ',
    'serial_like': 'シリアル番号らしき文字列',
    'address_like': '住所・郵便番号らしき文字列',
    'network_host': '接続先・ネットワーク識別子',
    'customer_field': '顧客・連絡先フィールド',
    'secret_like': '秘密情報らしきフィールド',
    'credential_field': '資格情報フィールド',
}


def scope_label(scope: PreflightScope) -> str:
    return _SCOPE_LABELS[scope]


def export_kind_label(export_kind: str) -> str:
    return _EXPORT_KIND_LABELS.get(export_kind, export_kind)


def class_label(data_class: str) -> str:
    return _CLASS_LABELS.get(data_class, data_class)


def rights_label(rights_class: str) -> str:
    return _RIGHTS_LABELS.get(rights_class, rights_class)


def risk_label(flag: str) -> str:
    return _RISK_LABELS.get(flag, flag)


# ---------------------------------------------------------------------------
# Sensitive-content heuristics — flags for human review, never a verdict.
# ---------------------------------------------------------------------------

_POSTCODE_RE = re.compile(r'〒\s*\d{3}-\d{4}|(?<![\d\-])\d{3}-\d{4}(?![\d\-])')
_ADDRESS_TAIL_RE = re.compile(
    r'(?:都|道|府|県|市|区|町|村).{0,12}(?:丁目|番地|番|号)'
    r'|(?:都|道|府|県).{0,20}?(?:区|市|町|村)\d'
)
_PHONE_RE = re.compile(r'\b0\d{1,4}-\d{1,4}-\d{4}\b')
_IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b')
_MAC_RE = re.compile(r'\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b')
_SECRET_KEY_RE = re.compile(
    r'(?:password|passwd|secret|token|api_?key|credential|private_?key'
    r'|access_?key|証明書|パスワード|秘密鍵)',
    re.IGNORECASE,
)
_SERIAL_RE = re.compile(
    r'\b[A-Z]{1,3}\d{6,}\b|\b(?:S/?N|シリアル)\s*[:＃#]\s*\w+'
)
_CUSTOMER_KEY_RE = re.compile(
    r'(?:customer|client|contact|owner|address|phone|email|'
    r'顧客|客先|連絡先|住所|電話|担当者|メール)',
    re.IGNORECASE,
)
_CREDENTIAL_KEY_RE = re.compile(
    r'(?:pairing|certificate|cert_|_pin\b|passcode|認証|資格情報)',
    re.IGNORECASE,
)


def scan_sensitive_text(text: str) -> tuple[str, ...]:
    """Heuristic flags over payload text — review aids, not verdicts."""
    flags: set[str] = set()
    if _POSTCODE_RE.search(text) or _ADDRESS_TAIL_RE.search(text):
        flags.add('address_like')
    if _PHONE_RE.search(text):
        flags.add('address_like')
    if _IPV4_RE.search(text) or _MAC_RE.search(text):
        flags.add('network_host')
    if _SECRET_KEY_RE.search(text):
        flags.add('secret_like')
    if _SERIAL_RE.search(text):
        flags.add('serial_like')
    if _CUSTOMER_KEY_RE.search(text):
        flags.add('customer_field')
    if _CREDENTIAL_KEY_RE.search(text):
        flags.add('credential_field')
    return tuple(sorted(flags))


# ---------------------------------------------------------------------------
# Elements — one reviewable unit of the outgoing artifact.
# ---------------------------------------------------------------------------

#: Table-name fragments -> (JA kind label, proposed DataClass, base flags).
_TABLE_SENSITIVITY: tuple[tuple[str, str, DataClass, tuple[str, ...]], ...] = (
    ('credential', '資格情報', 'credential_or_secret', ('credential_field',)),
    ('secret', '資格情報', 'credential_or_secret', ('credential_field',)),
    ('pairing', 'ペアリング・認証情報', 'credential_or_secret', ('credential_field',)),
    ('vault', '資格情報', 'credential_or_secret', ('credential_field',)),
    ('measurement_assets', '測定アセット', 'client_confidential', ('raw_recording',)),
    ('measurement', '測定データ', 'client_confidential', ('raw_recording',)),
    ('frequency_response', '生測定データ', 'client_confidential', ('raw_recording',)),
    ('raw_mesh', '生スキャン・メッシュ', 'personal_pii', ('raw_mesh', 'photo')),
    ('mesh', '生スキャン・メッシュ', 'personal_pii', ('raw_mesh',)),
    ('photo', '写真・画像', 'personal_pii', ('photo',)),
    ('scan', 'スキャンデータ', 'personal_pii', ('raw_mesh', 'photo')),
    ('image', '写真・画像', 'personal_pii', ('photo',)),
    ('customer', '顧客情報', 'personal_pii', ('customer_field',)),
    ('client', '顧客情報', 'personal_pii', ('customer_field',)),
    ('contact', '連絡先', 'personal_pii', ('customer_field',)),
    ('network', 'ネットワーク・接続先', 'security_sensitive', ('network_host',)),
    ('endpoint', '接続先・エンドポイント', 'security_sensitive', ('network_host',)),
    ('destination', '接続先情報', 'security_sensitive', ('network_host',)),
    ('equipment', '機材・シリアル情報', 'client_confidential', ('serial_like',)),
    ('device', '機器情報', 'client_confidential', ('serial_like',)),
    ('catalog', '機材カタログ', 'internal_project', ()),
    ('scene_revisions', 'シーン・設計権威', 'proprietary_license_restricted', ()),
    ('brief', '設計概要', 'internal_project', ()),
    ('presentation', 'プレゼン資料', 'internal_project', ()),
)


def _table_sensitivity(table: str) -> tuple[str, DataClass, tuple[str, ...]]:
    lowered = table.lower()
    for fragment, label, data_class, flags in _TABLE_SENSITIVITY:
        if fragment in lowered:
            return label, data_class, flags
    return 'プロジェクトデータ', 'internal_project', ()


@dataclass
class PreflightElement:
    """One reviewable unit — a bundle table, managed asset or member file."""

    element_id: str            # 'table:<name>' | 'asset:<sha>' | 'member:<name>'
    kind: str                  # 'table' | 'asset' | 'member'
    label: str                 # JA kind label
    detail: str                # what it is (rows / filename / member role)
    size_bytes: int
    content_sha256: str        # pins the exact reviewed content
    proposed_class: DataClass = 'internal_project'
    risk_flags: tuple[str, ...] = ()
    stored: ProjectDataClassification | None = None
    #: operator-confirmed values (dialog mutates these)
    confirmed_class: DataClass | None = None
    confirmed_rights: RightsClass | None = None
    include: bool = True
    locked: bool = False
    lock_reason: str | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    #: Filename this element produces in the written artifact
    #: (for post-export member inspection); ``None`` when n/a.
    file_name: str | None = None

    def artifact_ref(self) -> AuthorityRef:
        return AuthorityRef(
            kind='export_element',
            ref_id=self.element_id,
            ref_sha256=self.content_sha256,
        )

    def effective_class(self) -> DataClass | None:
        if self.confirmed_class is not None:
            return self.confirmed_class
        if self.stored is not None:
            return self.stored.data_class
        return None

    def effective_rights(self) -> RightsClass:
        if self.confirmed_rights is not None:
            return self.confirmed_rights
        if self.stored is not None:
            return self.stored.rights_class
        return 'undeclared'

    def persisted_classification(self) -> ProjectDataClassification | None:
        """The classification record to evaluate — stored or confirmed."""
        if self.stored is not None:
            return self.stored
        return None


@dataclass
class PreflightPlan:
    """Everything the preflight dialog reviews for one export."""

    export_kind: str
    document_id: str
    source_revision_id: str | None
    source_sha256: str | None
    elements: list[PreflightElement]
    policy: SensitiveArtifactPolicy | None = None
    manifest: ExportRedactionManifest | None = None
    member_exclusion_supported: bool = True
    payload: object = None  # the surface-specific write plan / members

    def included(self) -> list[PreflightElement]:
        return [element for element in self.elements if element.include]

    def expected_member_names(self) -> list[str]:
        """Filenames the included elements will produce on write."""
        return [
            element.file_name
            for element in self.included()
            if element.file_name
        ]

    def excluded(self) -> list[PreflightElement]:
        return [element for element in self.elements if not element.include]

    def total_bytes(self) -> int:
        return sum(element.size_bytes for element in self.included())

    def reproducibility(self) -> Literal['intact', 'degraded']:
        return 'degraded' if self.excluded() else 'intact'


# Flags that make post-export inspection report a finding. The
# remaining flags (customer_field …) are review aids only — generic
# keys like ``owner``/``contact`` appear in sealed model structure
# and must not mark a clean export as sensitive.
_FINDING_FLAGS = frozenset({
    'address_like', 'network_host', 'secret_like', 'credential_field',
    'serial_like', 'photo',
})


# ---------------------------------------------------------------------------
# Analyzers — enumerate exactly what would leave the project.
# ---------------------------------------------------------------------------


def _element_sha(*parts: str) -> str:
    return canonical_sha256({'preflight_element': list(parts)})


def analyze_bundle_plan(plan, stored: dict[str, ProjectDataClassification]):
    """Elements from a ``BundleWritePlan`` — tables + managed assets."""
    elements: list[PreflightElement] = []
    payload_text = {
        name: body.decode('utf-8', errors='replace')
        for name, body in plan.db_payloads.items()
    }
    for summary in plan.manifest.tables:
        label, proposed, base_flags = _table_sensitivity(summary.table)
        member = f'db/{summary.table}.jsonl'
        flags = set(base_flags)
        flags.update(scan_sensitive_text(payload_text.get(member, '')))
        element_id = f'table:{summary.table}'
        elements.append(
            PreflightElement(
                element_id=element_id,
                kind='table',
                label=label,
                detail=f'{summary.table}（{summary.row_count} 行）',
                size_bytes=len(plan.db_payloads.get(member, b'')),
                content_sha256=summary.rows_sha256,
                proposed_class=proposed,
                risk_flags=tuple(sorted(flags)),
                stored=stored.get(element_id),
                file_name=member,
            )
        )
    filename_by_digest = getattr(plan, 'asset_filenames', {})
    for entry in plan.manifest.assets:
        media = entry.media_type or ''
        flags: set[str] = set()
        if media.startswith('image/'):
            flags.update(('photo',))
            proposed = 'personal_pii'
        elif media.startswith('audio/'):
            flags.update(('raw_recording',))
            proposed = 'client_confidential'
        else:
            proposed = 'internal_project'
        filename = filename_by_digest.get(entry.sha256, '')
        element_id = f'asset:{entry.sha256}'
        elements.append(
            PreflightElement(
                element_id=element_id,
                kind='asset',
                label='アセット（'
                + (filename or media or 'バイナリ')
                + '）',
                detail=(
                    f'{entry.sha256[:12]}…'
                    + (f' — {filename}' if filename else '')
                ),
                size_bytes=entry.size_bytes,
                content_sha256=entry.sha256,
                proposed_class=proposed,
                risk_flags=tuple(sorted(flags)),
                stored=stored.get(element_id),
                file_name=f'assets/{entry.sha256}',
            )
        )
    return elements


_MEMBER_KIND_LABELS = {
    '.csv': 'CSV',
    '.json': 'JSON',
    '.html': 'HTML',
    '.png': '画像',
    '.jpg': '画像',
    '.jpeg': '画像',
}


def analyze_member_files(
    members: dict[str, str | bytes],
    stored: dict[str, ProjectDataClassification],
    *,
    kind_label: str = '出力ファイル',
    filename_map: dict[str, str] | None = None,
) -> list[PreflightElement]:
    """Elements from generated member contents (member key -> text/bytes).

    ``filename_map`` translates member keys to their output filenames
    for post-export inspection; without it the key IS the filename.
    """
    elements: list[PreflightElement] = []
    for name in sorted(members):
        body = members[name]
        raw = body.encode('utf-8') if isinstance(body, str) else body
        text = (
            body
            if isinstance(body, str)
            else raw[:65536].decode('utf-8', errors='replace')
        )
        suffix = Path(name).suffix.lower()
        flags = set(scan_sensitive_text(text))
        if suffix in ('.png', '.jpg', '.jpeg'):
            flags.add('photo')
            proposed: DataClass = 'personal_pii'
        else:
            proposed = 'internal_project'
        element_id = f'member:{name}'
        file_name = (filename_map or {}).get(name, name)
        elements.append(
            PreflightElement(
                element_id=element_id,
                kind='member',
                label=kind_label,
                detail=file_name,
                size_bytes=len(raw),
                content_sha256=_element_sha(element_id, str(len(raw)),
                                            canonical_sha256(text[:8192])),
                proposed_class=proposed,
                risk_flags=tuple(sorted(flags)),
                stored=stored.get(element_id),
                file_name=file_name,
            )
        )
    return elements


def analyze_member_categories(
    categories: list[tuple[str, str, int, DataClass, tuple[str, ...]]],
    stored: dict[str, ProjectDataClassification],
) -> list[PreflightElement]:
    """Elements from (element_id, detail, size, proposed, flags) rows —
    for packages whose members are generated, not enumerable up front."""
    elements: list[PreflightElement] = []
    for element_id, detail, size, proposed, flags in categories:
        elements.append(
            PreflightElement(
                element_id=element_id,
                kind='member',
                label='パッケージ内容',
                detail=detail,
                size_bytes=size,
                content_sha256=_element_sha(element_id, detail, str(size)),
                proposed_class=proposed,
                risk_flags=tuple(flags),
                stored=stored.get(element_id),
            )
        )
    return elements


# ---------------------------------------------------------------------------
# Authority — persisted classifications, policy, manifest, eligibility.
# ---------------------------------------------------------------------------


def stored_classification_map(
    repository: CadCodePolicyRepository, document_id: str
) -> dict[str, ProjectDataClassification]:
    """Latest classification per element ref_id for this document."""
    found: dict[str, ProjectDataClassification] = {}
    for record in repository.data_classifications.list(document_id):
        found[record.artifact_ref.ref_id] = record
    return found


_EXPORTABLE_CLASSES: tuple[DataClass, ...] = (
    'public_shareable',
    'internal_project',
    'client_confidential',
    'personal_pii',
    'security_sensitive',
    'proprietary_license_restricted',
    'safety_engineering_restricted',
)


def ensure_export_policy(
    repository: CadCodePolicyRepository, document_id: str
) -> SensitiveArtifactPolicy:
    """The document's export/share policy for reviewed classes (#989).

    Recorded once, reused afterwards: the project owner may export and
    share artifacts whose class they reviewed — the canonical model
    itself refuses export/share grants over credential_or_secret and
    unknown_classification, so those can never be covered here.
    """
    for policy in repository.artifact_policies.list(document_id):
        if policy.allow_export_roles or policy.allow_share_roles:
            return policy
    policy = SensitiveArtifactPolicy.create(
        document_id=document_id,
        target_classes=_EXPORTABLE_CLASSES,
        allow_view_roles=('owner',),
        allow_edit_roles=('owner',),
        allow_export_roles=('owner',),
        allow_share_roles=('owner',),
        derived_inherits=True,
        min_bundle_profile='include_raw_measurement',
    )
    repository.save_artifact_policy(policy)
    return policy


def record_confirmations(
    repository: CadCodePolicyRepository,
    plan: PreflightPlan,
) -> tuple[ProjectDataClassification, ...]:
    """Persist operator-confirmed classifications as approved records.

    Only elements the operator explicitly reviewed get a record;
    unconfirmed elements keep ``stored=None`` and fail closed later.
    A stored record for the same ref is left authoritative (append-only).
    """
    written: list[ProjectDataClassification] = []
    for element in plan.elements:
        if element.stored is not None:
            continue
        if element.confirmed_class is None:
            continue
        record = ProjectDataClassification.create(
            document_id=plan.document_id,
            artifact_ref=element.artifact_ref(),
            data_class=element.confirmed_class,
            rights_class=element.confirmed_rights or 'undeclared',
            review_state='approved',
            rationale=(
                'export preflight: classification confirmed by the '
                'project owner before export'
                + (
                    f' ({element.confirmed_class})'
                    if element.confirmed_class == 'credential_or_secret'
                    else ''
                )
            ),
        )
        repository.save_data_classification(record)
        element.stored = record
        written.append(record)
    return tuple(written)


def build_manifest(
    repository: CadCodePolicyRepository,
    plan: PreflightPlan,
    *,
    bundle_kind: str,
    policy: SensitiveArtifactPolicy,
) -> ExportRedactionManifest:
    """The allowlist manifest for an approved external selection (#722).

    Empty allowlists cannot be manifested — the export is blocked, which
    is the honest answer when nothing is eligible for external sharing.
    """
    included = plan.included()
    if not included:
        raise ValueError(
            '外部送付できる項目がありません — 出力を停止します'
        )
    manifest = ExportRedactionManifest.create(
        document_id=plan.document_id,
        bundle_kind=bundle_kind,
        included_refs=tuple(element.artifact_ref() for element in included),
        excluded_refs=tuple(
            element.artifact_ref() for element in plan.excluded()
        ),
        redactions=(),
        policy_ref=AuthorityRef(
            kind='sensitive_artifact_policy',
            ref_id=policy.policy_id,
            ref_sha256=policy.policy_sha256,
        ),
    )
    repository.save_export_manifest(manifest)
    plan.manifest = manifest
    return manifest


def evaluate_elements(plan: PreflightPlan) -> list[str]:
    """Canonical eligibility per element; returns blocking reasons (#722).

    Every element the operator marked for sending must pass
    ``evaluate_export_eligibility`` — a single failure stops the export.
    """
    blockers: list[str] = []
    for element in plan.elements:
        verdict, reason = evaluate_export_eligibility(
            element.persisted_classification(),
            plan.policy,
            plan.manifest,
            element.artifact_ref(),
        )
        element.verdict = verdict
        element.verdict_reason = reason
        if element.include and verdict != 'export_allowed_within_manifest':
            blockers.append(
                f'{element.element_id}: {verdict} ({reason})'
            )
    return blockers


def lock_ineligible(element: PreflightElement) -> str | None:
    """Why this element cannot be externally sent without confirmation —
    shown in the dialog; ``None`` means includable after confirmation."""
    data_class = element.effective_class()
    if data_class == 'credential_or_secret':
        return '秘密情報は外部出力できません'
    if data_class in (None, 'unknown_classification'):
        return '区分が未確定です — 確認するまで送付できません'
    if element.effective_rights() in (
        'undeclared', 'licensed_no_redistribution'
    ):
        return '権利が未確認・再配布不可です'
    return None


# ---------------------------------------------------------------------------
# Verdict + inspection — post-export honesty.
# ---------------------------------------------------------------------------

VERDICT_FILENAME = 'preflight-verdict.json'
BUNDLE_VERDICT_MEMBER = 'privacy/preflight-verdict.json'


def verdict_payload(
    plan: PreflightPlan,
    *,
    scope: PreflightScope,
    destination: str,
    written_members: list[str],
    inspection: dict | None = None,
) -> dict:
    """The privacy verdict/evidence record saved after export (#989)."""
    return {
        'schema': 'htdt.export-preflight-verdict',
        'version': '1.0.0',
        'export_kind': plan.export_kind,
        'scope': scope,
        'scope_label': scope_label(scope),
        'document_id': plan.document_id,
        'source_revision_id': plan.source_revision_id,
        'source_sha256': plan.source_sha256,
        'manifest_id': (
            plan.manifest.manifest_id if plan.manifest else None
        ),
        'manifest_sha256': (
            plan.manifest.manifest_sha256 if plan.manifest else None
        ),
        'policy_id': plan.policy.policy_id if plan.policy else None,
        'reproducibility': plan.reproducibility(),
        'included': [
            {
                'element_id': element.element_id,
                'verdict': element.verdict,
                'size_bytes': element.size_bytes,
            }
            for element in plan.included()
        ],
        'excluded': [
            {
                'element_id': element.element_id,
                'verdict': element.verdict,
                'reason': element.verdict_reason or element.lock_reason,
            }
            for element in plan.excluded()
        ],
        'destination': destination,
        'written_members': written_members,
        'inspection': inspection or {},
    }


def write_verdict_sidecar(
    destination: Path, payload: dict
) -> Path:
    """``<output>.preflight-verdict.json`` beside the written artifact."""
    path = Path(str(destination) + '.preflight-verdict.json')
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    return path


def inspect_exported(
    destination: Path,
    *,
    expected_members: list[str],
    scan_text: bool = True,
) -> dict:
    """Post-export check: member set matches the manifest; no raw leaks.

    For archives the member list must equal the approved selection
    (plus ``manifest.json``/``privacy/*`` bookkeeping members). For
    member directories the written files must match too. Text members
    are re-scanned for secret-like content — findings are reported,
    never hidden.
    """
    destination = Path(destination)
    result: dict = {
        'destination': str(destination),
        'expected_members': sorted(expected_members),
        'actual_members': [],
        'unexpected_members': [],
        'missing_members': [],
        'sensitive_findings': [],
        'verdict': 'ok',
    }
    allowed_extra = {'manifest.json', BUNDLE_VERDICT_MEMBER,
                     'privacy/preflight-verdict.json'}
    actual: list[str] = []
    try:
        if destination.suffix == '.htdtproject' or zipfile.is_zipfile(
            destination
        ):
            with zipfile.ZipFile(destination) as archive:
                actual = sorted(archive.namelist())
                if scan_text:
                    for name in archive.namelist():
                        if name.endswith(('.jsonl', '.json', '.csv',
                                          '.html', '.txt')):
                            text = archive.read(name)[:262144].decode(
                                'utf-8', errors='replace'
                            )
                            flags = scan_sensitive_text(text)
                            if set(flags) & _FINDING_FLAGS:
                                result['sensitive_findings'].append(
                                    {'member': name, 'flags': list(flags)}
                                )
        elif destination.is_dir():
            actual = sorted(
                str(child.relative_to(destination)).replace('\\', '/')
                for child in destination.rglob('*')
                if child.is_file()
            )
            if scan_text:
                for child in destination.rglob('*'):
                    if child.is_file() and child.suffix in (
                        '.jsonl', '.json', '.csv', '.html', '.txt'
                    ):
                        text = child.read_text(
                            encoding='utf-8', errors='replace'
                        )[:262144]
                        flags = scan_sensitive_text(text)
                        if set(flags) & _FINDING_FLAGS:
                            result['sensitive_findings'].append(
                                {
                                    'member': str(
                                        child.relative_to(destination)
                                    ),
                                    'flags': list(flags),
                                }
                            )
        else:
            actual = [destination.name]
            if scan_text:
                try:
                    text = destination.read_text(
                        encoding='utf-8', errors='replace'
                    )[:262144]
                    flags = scan_sensitive_text(text)
                    if flags:
                        result['sensitive_findings'].append(
                            {'member': destination.name,
                             'flags': list(flags)}
                        )
                except OSError:
                    pass
    except (OSError, zipfile.BadZipFile) as exc:
        result['verdict'] = f'inspection_failed: {exc}'
        return result

    result['actual_members'] = actual
    expected = set(expected_members) | allowed_extra
    unexpected = [name for name in actual if name not in expected]
    missing = [
        name for name in expected_members if name not in set(actual)
    ]
    result['unexpected_members'] = unexpected
    result['missing_members'] = missing
    if unexpected or missing:
        result['verdict'] = 'member_mismatch'
    elif result['sensitive_findings']:
        result['verdict'] = 'sensitive_findings'
    return result


__all__ = [
    'BUNDLE_VERDICT_MEMBER',
    'PreflightElement',
    'PreflightPlan',
    'PreflightScope',
    'VERDICT_FILENAME',
    'analyze_bundle_plan',
    'analyze_member_categories',
    'analyze_member_files',
    'build_manifest',
    'class_label',
    'ensure_export_policy',
    'evaluate_elements',
    'export_kind_label',
    'inspect_exported',
    'lock_ineligible',
    'record_confirmations',
    'rights_label',
    'risk_label',
    'scan_sensitive_text',
    'scope_label',
    'stored_classification_map',
    'verdict_payload',
    'write_verdict_sidecar',
]
