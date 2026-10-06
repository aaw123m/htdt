"""Manifest-to-verification bridge — seal each issue_verification_manifest
check as an addressable gate so automated runs and committed device evidence
share one fail-closed closure ledger.

The manifest (``scripts/issue_verification_manifest.yaml``) already declares,
per open issue, which pytest/script checks honestly evidence progress and which
manual/physical evidence automation can never produce. This module pins each
declared check as a sealed ``ManifestGate`` (content-hash of the manifest
file included, so editing the manifest re-keys every gate), records each run
or physical evidence commit as a ``GateRunResult``, and evaluates the
manifest's own verdict rule — ``verified`` only when every automated check
passed AND every manual gate has bound evidence; never asserted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_verification_automation import (
    RequiredCell,
    VerificationRequirement,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

_CHECK_KINDS = ('pytest', 'script', 'manual')


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


class ManifestGate(BaseModel):
    """One manifest check sealed as an addressable gate. ``manifest_sha256``
    pins the file bytes so any manifest edit invalidates the pin."""

    model_config = ConfigDict(frozen=True)

    gate_id: str
    gate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    issue_ref: str
    check_id: str
    check_kind: Literal['pytest', 'script', 'manual']
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    description: str
    tests: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    cells: tuple[RequiredCell, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('issue_ref'):
                raise ValueError('a gate needs its issue ref')
            if not data.get('check_id'):
                raise ValueError('a gate needs its check id')
            if data.get('check_kind') not in _CHECK_KINDS:
                raise ValueError('unknown manifest check kind')
            if not data.get('description'):
                raise ValueError('a gate needs its description')
            kind = data.get('check_kind')
            if kind == 'pytest' and not data.get('tests'):
                raise ValueError('a pytest gate needs test targets')
            if kind == 'script' and not data.get('argv'):
                raise ValueError('a script gate needs an argv list')
            if kind != 'manual' and data.get('cells'):
                raise ValueError(
                    'device cells only belong on manual gates — '
                    'automated checks run without acquisition'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'gate_id', 'gate_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ManifestGate':
        return _seal(cls, payload, 'gate_id', 'gate_sha256', 'mgt')


class GateRunResult(BaseModel):
    """Sealed record of one gate execution or one physical-evidence
    commit. ``evidence_committed`` is the only manual outcome that
    satisfies a gate, and it requires a bound evidence ref."""

    model_config = ConfigDict(frozen=True)

    result_id: str
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    gate_ref: AuthorityRef
    outcome: Literal[
        'passed', 'failed', 'error', 'timeout',
        'evidence_committed', 'pending',
    ]
    evidence_ref: AuthorityRef | None = None
    finished_at_utc: str = ''
    detail: str = ''

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('gate_ref') is None:
                raise ValueError('a run result needs its gate ref')
            if data.get('outcome') not in (
                'passed', 'failed', 'error', 'timeout',
                'evidence_committed', 'pending',
            ):
                raise ValueError('unknown gate outcome')
            if data.get('outcome') == 'evidence_committed' and (
                data.get('evidence_ref') is None
            ):
                raise ValueError(
                    'committed physical evidence needs its '
                    'evidence ref — a bare claim binds nothing'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'result_id', 'result_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'GateRunResult':
        return _seal(cls, payload, 'result_id', 'result_sha256', 'grr')


def load_manifest_gates(
    document_id: str, manifest_path: str | Path
) -> tuple[ManifestGate, ...]:
    """Parse the issue-verification manifest into sealed gates.

    Fail-closed: unknown check kinds, missing ids, and shapeless
    entries raise; the file's own sha256 pins every gate it yields.
    """
    path = Path(manifest_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(doc, dict) or not isinstance(
        doc.get('issues'), list
    ):
        raise ValueError('manifest has no issues list')
    gates: list[ManifestGate] = []
    for issue_entry in doc['issues']:
        if not isinstance(issue_entry, dict):
            raise ValueError('manifest issue entry is not a mapping')
        number = issue_entry.get('issue')
        if number is None:
            raise ValueError('manifest issue entry lacks a number')
        checks = issue_entry.get('checks') or ()
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError(
                    f'issue {number}: check is not a mapping'
                )
            cells = tuple(
                RequiredCell(**c) for c in check.get('cells') or ()
            )
            gates.append(
                ManifestGate.create(
                    {
                        'document_id': document_id,
                        'issue_ref': f'issue-{number}',
                        'check_id': check.get('id'),
                        'check_kind': check.get('kind'),
                        'manifest_sha256': digest,
                        'description': check.get('description') or '',
                        'tests': tuple(check.get('tests') or ()),
                        # The manifest's script field is ``command``
                        # (verify_open_issues.py); ``argv`` stays
                        # accepted for the synthetic manifests used by
                        # callers/tests that predate the real file.
                        'argv': tuple(
                            check.get('command') or check.get('argv') or ()
                        ),
                        'cells': cells,
                    }
                )
            )
    return tuple(gates)


def derive_verification_requirement(
    gate: ManifestGate,
) -> VerificationRequirement | None:
    """A manual gate with declared acquisition cells derives a sealed
    VerificationRequirement — the device run becomes a generated
    checklist, not hand-authored bookkeeping. A gate without cells is
    honest manual procedure and stays a plain gate."""
    if gate.check_kind != 'manual' or not gate.cells:
        return None
    return VerificationRequirement.create(
        {
            'document_id': gate.document_id,
            'issue_ref': gate.issue_ref,
            'gate_label': gate.check_id,
            'required_cells': tuple(
                c.model_dump(mode='python') for c in gate.cells
            ),
            'evaluator_kind': 'manual_review',
        }
    )


GateVerdict = Literal[
    'satisfied',
    'unsatisfied',
    'unevaluated',
]


def evaluate_gate(
    gate: ManifestGate, result: GateRunResult | None
) -> tuple[GateVerdict, str]:
    """One gate's satisfaction — automated checks satisfy only on a
    passed run; manual gates satisfy only on bound committed
    evidence."""
    if result is None or result.outcome == 'pending':
        return 'unevaluated', 'gate has no finished run'
    if result.gate_ref.ref_id != gate.gate_id:
        return 'unsatisfied', 'result is bound to a different gate'
    if gate.check_kind == 'manual':
        if result.outcome == 'evidence_committed':
            return 'satisfied', 'physical evidence committed and bound'
        return (
            'unsatisfied',
            'manual gate: only committed evidence satisfies — '
            'a passing script cannot substitute for the physical step',
        )
    if result.outcome == 'passed':
        return 'satisfied', 'automated check passed'
    return 'unsatisfied', f'automated check outcome {result.outcome}'


IssueVerdict = Literal[
    'verified',
    'partially_verified',
    'failing',
    'manual_required',
    'unevaluated',
]


def evaluate_issue_verdict(
    gates: tuple[ManifestGate, ...],
    results: dict[str, GateRunResult],
) -> tuple[IssueVerdict, str]:
    """The manifest's own rule, evaluated on sealed records:
    ``failing`` if any automated check failed; ``manual_required``
    when nothing automated exists; ``verified`` only when every
    automated check passed and no manual gate lacks evidence;
    otherwise ``partially_verified``."""
    if not gates:
        return 'unevaluated', 'issue has no declared gates'
    auto = [g for g in gates if g.check_kind != 'manual']
    manual = [g for g in gates if g.check_kind == 'manual']
    if not auto:
        return 'manual_required', 'no automated checks declared'
    for g in auto:
        v, _ = evaluate_gate(g, results.get(g.gate_id))
        if v == 'unsatisfied':
            return 'failing', f'check {g.check_id} failed'
    manual_open = [
        g.check_id
        for g in manual
        if evaluate_gate(g, results.get(g.gate_id))[0]
        != 'satisfied'
    ]
    auto_open = [
        g.check_id
        for g in auto
        if evaluate_gate(g, results.get(g.gate_id))[0]
        == 'unevaluated'
    ]
    if manual_open or auto_open:
        missing = ', '.join(manual_open + auto_open)
        return (
            'partially_verified',
            f'automated checks green so far; open gates: {missing}',
        )
    return (
        'verified',
        'every automated check passed and every manual gate has '
        'bound evidence',
    )


MANIFEST_VERDICT_LABELS: dict[str, str] = {
    'verified': '検証済み',
    'partially_verified': '一部検証済み',
    'failing': '失敗あり',
    'manual_required': '実機検証が必要',
    'unevaluated': '未評価',
}
