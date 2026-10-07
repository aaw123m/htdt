"""File-export deployment evidence authority (#838 slice A).

File-based DSP targets — Equalizer APO, generic PEQ/biquad/FIR exports,
manual AVR handoff — have no canonical machine-verifiable runtime state
in the reviewed public documentation. Equalizer APO's own documentation
states unsupported command names or nonconforming lines may be silently
ignored, so a written file is not a verified deployment:

    config file written != config semantically accepted
    != requested transfer active

This module seals the *honest* evidence chain for such targets:

* ``file_state`` tracks file-level truth only —
  ``exported_config`` → ``file_installed`` → ``file_readback_matched`` /
  ``file_readback_mismatch`` — plus ``export_blocked`` for fail-closed
  rejects and ``unknown`` when nothing is established.
* ``runtime_state`` has exactly two values: ``runtime_not_attested``
  (the default — file truth never implies runtime truth) and
  ``post_measurement_verified`` (reachable only by binding
  post-deployment measurement refs). There is no ``runtime_verified``
  state a file can reach; post-deployment measurement remains the final
  operational verification.
* ``evidence_mode`` keeps strengths distinct — ``file_level_readback``
  (HTDT re-parsed the written file and matched semantics) vs
  ``operator_attestation`` (a person asserts install/entry) vs
  ``unverified_export`` (a file exists, nothing else is known). Manual
  and file handoff never masquerade as machine readback.

Unsupported target parameters fail closed at render time (the renderer
raises / the record seals ``export_blocked`` with reasons) — HTDT never
mirrors Equalizer APO's silent-ignore behavior.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError(f'{label} must be an ISO-8601 UTC timestamp')


FileExportTargetClass = Literal[
    'equalizer_apo_config', 'generic_peq_export', 'generic_biquad_export',
    'generic_fir_export', 'manual_avr_settings', 'other_file_target',
    'unknown',
]
"""File/manual deployment target families. ``manual_avr_settings`` is the
operator-entry handoff; the generic exports are the always-available
fallbacks for targets without a qualified adapter (#838 action 6)."""

FileExportState = Literal[
    'exported_config', 'file_installed', 'file_readback_matched',
    'file_readback_mismatch', 'export_blocked', 'unknown',
]
"""File-level truth only — never a runtime claim."""

FileExportRuntimeState = Literal[
    'runtime_not_attested', 'post_measurement_verified',
]
"""The runtime ceiling: file evidence alone can never attest runtime
state, so the only promotion is post-deployment measurement binding."""

FileExportEvidenceMode = Literal[
    'file_level_readback', 'operator_attestation', 'unverified_export',
]
"""Evidence strength, ordered weakest→strongest for file truth:
``unverified_export`` (file produced), ``operator_attestation`` (a human
asserts install/entry), ``file_level_readback`` (the written file was
re-parsed and semantically matched). None of these is a runtime claim;
manual attestation stays visibly weaker than file readback."""

RoundtripVerdict = Literal['matched', 'mismatch', 'not_performed']


FILE_EXPORT_LABELS: dict[str, str] = {
    'equalizer_apo_config': 'Equalizer APO 構成ファイル',
    'generic_peq_export': '汎用 PEQ エクスポート',
    'generic_biquad_export': '汎用バイクアッドエクスポート',
    'generic_fir_export': '汎用 FIR エクスポート',
    'manual_avr_settings': 'AVR 手動設定ハンドオフ',
    'other_file_target': 'その他ファイルターゲット',
    'unknown': '不明',
    'exported_config': '設定ファイル生成済み',
    'file_installed': 'ファイル配置済み',
    'file_readback_matched': 'ファイル読み戻し一致',
    'file_readback_mismatch': 'ファイル読み戻し不一致',
    'export_blocked': 'エクスポート不可（未対応パラメータ）',
    'runtime_not_attested': 'ランタイム未証明（ファイル一致まで）',
    'post_measurement_verified': '再測定で検証済み',
    'file_level_readback': 'ファイルレベル読み戻し',
    'operator_attestation': 'オペレータ証言',
    'unverified_export': '未検証エクスポート',
    'matched': '一致',
    'mismatch': '不一致',
    'not_performed': '未実施',
}


class FileExportDeployment(BaseModel):
    """Sealed file-deployment evidence for one rendered config (fed-).

    Pins the exact exported bytes hash, the renderer identity, the target
    scope the file was written for, the file-level truth, and the runtime
    state ceiling. The model structurally cannot claim a verified runtime:
    ``runtime_state`` only ever reaches ``post_measurement_verified``
    through bound post-deployment measurement evidence.
    """

    model_config = ConfigDict(frozen=True)

    file_deployment_id: str
    file_deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    target_class: FileExportTargetClass = 'unknown'
    #: The device/channel scope the file was rendered for (repr text —
    #: retained identity, not a canonical target claim).
    target_scope_repr: str = Field(min_length=1)
    #: Exact sha256 of the rendered file bytes.
    exported_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    rendered_format_id: str = Field(min_length=1)
    file_state: FileExportState = 'unknown'
    runtime_state: FileExportRuntimeState = 'runtime_not_attested'
    evidence_mode: FileExportEvidenceMode = 'unverified_export'
    roundtrip_verdict: RoundtripVerdict = 'not_performed'
    #: Where the file was placed, when an install is claimed.
    file_path_repr: str | None = None
    install_attestor: str | None = None
    post_measurement_refs: tuple[AuthorityRef, ...] = ()
    block_reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'FileExportDeployment':
        _require_refs(*self.post_measurement_refs)
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')

        if self.file_state == 'export_blocked':
            if not self.block_reasons:
                raise ValueError(
                    'export_blocked requires block_reasons')
            if self.post_measurement_refs:
                raise ValueError(
                    'a blocked export cannot pin post measurements')
        if self.file_state == 'file_installed':
            if self.evidence_mode == 'unverified_export':
                raise ValueError(
                    'file_installed requires install evidence '
                    '(file_level_readback or operator_attestation)')
            if self.evidence_mode == 'operator_attestation' \
                    and not self.install_attestor:
                raise ValueError(
                    'operator-attested install requires install_attestor')
        if self.file_state in (
                'file_readback_matched', 'file_readback_mismatch'):
            if self.evidence_mode != 'file_level_readback':
                raise ValueError(
                    'readback states require evidence_mode '
                    "='file_level_readback'")
            expected = (
                'matched' if self.file_state == 'file_readback_matched'
                else 'mismatch')
            if self.roundtrip_verdict != expected:
                raise ValueError(
                    f'{self.file_state} requires '
                    f'roundtrip_verdict={expected!r}')
        if self.roundtrip_verdict in ('matched', 'mismatch') \
                and self.evidence_mode != 'file_level_readback':
            raise ValueError(
                'a performed roundtrip requires '
                "evidence_mode='file_level_readback'")
        if self.runtime_state == 'post_measurement_verified':
            if not self.post_measurement_refs:
                raise ValueError(
                    'post_measurement_verified requires bound '
                    'post-deployment measurement evidence')
            if self.file_state != 'file_readback_matched':
                raise ValueError(
                    'post_measurement_verified requires a '
                    'readback-matched file — an unverified or mismatched '
                    'file can never verify runtime state')
        if self.runtime_state == 'runtime_not_attested' \
                and self.post_measurement_refs:
            raise ValueError(
                'runtime_not_attested cannot pin post measurements')
        if self.evidence_mode == 'operator_attestation' \
                and self.file_state in ('file_readback_matched',
                                        'file_readback_mismatch'):
            raise ValueError(
                'operator attestation can never produce a file '
                'readback verdict')
        if self.file_deployment_sha256 != _hash(self.identity_payload()):
            raise ValueError('FileExportDeployment hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'file_deployment_id', 'file_deployment_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'FileExportDeployment':
        return _seal(
            cls, payload, 'file_deployment_id',
            'file_deployment_sha256', 'fed')


def derive_file_export_state(
    *,
    roundtrip_verdict: RoundtripVerdict,
    install_evidence: Literal[
        'installed_readback', 'installed_attested', 'not_installed'],
    post_measurement_count: int = 0,
    export_blocked_reasons: tuple[str, ...] = (),
) -> tuple[FileExportState, FileExportRuntimeState,
           FileExportEvidenceMode]:
    """Strongest honest (file_state, runtime_state, evidence_mode) the
    evidence supports — never stronger.

    * blocked render evidence → ``export_blocked``;
    * a semantic readback mismatch → ``file_readback_mismatch``;
    * matched readback + bound post measurement →
      ``file_readback_matched`` / ``post_measurement_verified``;
    * matched readback without post measurement →
      ``file_readback_matched`` / ``runtime_not_attested`` — the ceiling;
    * operator-attested install → ``file_installed`` /
      ``operator_attestation`` (readback verdicts stay impossible);
    * a produced file alone → ``exported_config`` /
      ``unverified_export``.
    """
    if export_blocked_reasons:
        return ('export_blocked', 'runtime_not_attested',
                'unverified_export')
    if roundtrip_verdict == 'mismatch':
        return ('file_readback_mismatch', 'runtime_not_attested',
                'file_level_readback')
    if roundtrip_verdict == 'matched':
        if post_measurement_count > 0:
            return ('file_readback_matched',
                    'post_measurement_verified', 'file_level_readback')
        return ('file_readback_matched', 'runtime_not_attested',
                'file_level_readback')
    if install_evidence == 'installed_readback':
        return ('file_installed', 'runtime_not_attested',
                'file_level_readback')
    if install_evidence == 'installed_attested':
        return ('file_installed', 'runtime_not_attested',
                'operator_attestation')
    return ('exported_config', 'runtime_not_attested',
            'unverified_export')


def evaluate_file_export_verification(
    record: FileExportDeployment | None,
) -> tuple[str, str]:
    """One-line honest verdict for a file-export deployment.

    Returns ``(verdict, reason)`` where verdict is the runtime ceiling the
    record supports — ``post_measurement_verified`` only when bound
    measurement evidence exists, ``runtime_not_attested`` for everything
    else that reached a matched file readback, and the file state itself
    for weaker chains.
    """
    if record is None:
        return ('unknown', 'no_file_export_deployment')
    if record.runtime_state == 'post_measurement_verified':
        return ('post_measurement_verified',
                'post_measurement_refs:'
                + str(len(record.post_measurement_refs)))
    if record.file_state == 'file_readback_matched':
        return ('runtime_not_attested',
                'file readback matched; runtime never attested without '
                'post-deployment measurement')
    if record.file_state == 'file_readback_mismatch':
        return ('file_readback_mismatch',
                'written file does not semantically match the request')
    if record.file_state == 'export_blocked':
        return ('export_blocked',
                'block_reasons:' + ','.join(record.block_reasons))
    return (record.file_state, f'file_state:{record.file_state}')


__all__ = [
    'FileExportDeployment',
    'FileExportEvidenceMode',
    'FILE_EXPORT_LABELS',
    'FileExportRuntimeState',
    'FileExportState',
    'FileExportTargetClass',
    'RoundtripVerdict',
    'derive_file_export_state',
    'evaluate_file_export_verification',
]
