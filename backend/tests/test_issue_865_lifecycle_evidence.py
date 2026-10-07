"""Issue #865 — lifecycle evidence-reference integrity.

Every repo-local ``evidence_ref`` in the issue lifecycle manifest must
resolve to an existing file under the repo root, and a landed/accepted
lifecycle state must retain at least one resolvable evidence artifact.
Missing, renamed, deleted, duplicated or repo-escaping references are
integrity failures — a ``SOFTWARE_LANDED``/``complete`` claim whose proof
does not resolve must fail closed, not coast on stale metadata.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'scripts'))

import issue_lifecycle as lc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_manifest(root: Path, entries_yaml: str) -> Path:
    manifest = root / 'scripts' / 'issue_lifecycle_manifest.yaml'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        'version: 1\nentries:\n' + entries_yaml, encoding='utf-8'
    )
    return manifest


def _entry(
    issue: int = 1,
    lifecycle: str = 'software_landed',
    refs: list[str] | None = None,
) -> str:
    refs_yaml = '[]' if refs is None else '[' + ', '.join(refs) + ']'
    return (
        f'- issue: {issue}\n'
        f'  lifecycle: {lifecycle}\n'
        '  landed: {prs: [1], commits: []}\n'
        '  remaining_gates: []\n'
        f'  evidence_refs: {refs_yaml}\n'
    )


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'docs' / 'issues').mkdir(parents=True)
    return root


def _codes(findings: list[lc.Finding]) -> set[str]:
    return {f.code for f in findings}


class TestEvidenceResolution:
    def test_valid_ref_resolves(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        (root / 'docs' / 'issues' / 'issue-1-doc.md').write_text('x', encoding='utf-8')
        manifest = _write_manifest(root, _entry(refs=['docs/issues/issue-1-doc.md']))
        entries = lc.load_lifecycle_manifest(manifest)
        assert lc.evidence_findings(entries, root) == []

    def test_missing_ref_is_error(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(root, _entry(refs=['docs/issues/gone.md']))
        entries = lc.load_lifecycle_manifest(manifest)
        findings = lc.evidence_findings(entries, root)
        assert _codes(findings) == {'evidence_ref_unresolvable'}
        finding = findings[0]
        assert finding.issue == 1
        assert finding.severity == 'error'
        assert 'software_landed' in finding.message
        assert 'docs/issues/gone.md' in finding.message

    def test_renamed_ref_detected(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        # File exists under the NEW name; the manifest still cites the old.
        (root / 'docs' / 'issues' / 'issue-1-renamed.md').write_text('x', encoding='utf-8')
        manifest = _write_manifest(root, _entry(refs=['docs/issues/issue-1-doc.md']))
        entries = lc.load_lifecycle_manifest(manifest)
        assert _codes(lc.evidence_findings(entries, root)) == {
            'evidence_ref_unresolvable'
        }

    def test_duplicate_ref_is_error(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        (root / 'docs' / 'issues' / 'issue-1-doc.md').write_text('x', encoding='utf-8')
        manifest = _write_manifest(
            root,
            _entry(refs=[
                'docs/issues/issue-1-doc.md',
                'docs/issues/issue-1-doc.md',
            ]),
        )
        entries = lc.load_lifecycle_manifest(manifest)
        assert 'evidence_ref_duplicate' in _codes(
            lc.evidence_findings(entries, root)
        )

    @pytest.mark.parametrize(
        'ref',
        [
            'C:/outside/file.md',
            '../sibling/file.md',
            'docs/../../secret.md',
            '/abs/path.md',
            '//server/share.md',
        ],
    )
    def test_repo_escaping_refs_rejected(
        self, tmp_path: Path, ref: str
    ) -> None:
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(root, _entry(refs=[ref]))
        entries = lc.load_lifecycle_manifest(manifest)
        assert _codes(lc.evidence_findings(entries, root)) == {
            'evidence_ref_not_repo_local'
        }

    def test_nonlanded_state_allows_empty_refs(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(
            root, _entry(lifecycle='in_progress', refs=[])
        )
        entries = lc.load_lifecycle_manifest(manifest)
        assert lc.evidence_findings(entries, root) == []

    @pytest.mark.parametrize(
        'lifecycle',
        [
            'software_landed',
            'acceptance_remaining',
            'physical_evidence_remaining',
            'structural_followup_remaining',
            'complete',
        ],
    )
    def test_landed_states_require_resolvable_evidence(
        self, tmp_path: Path, lifecycle: str
    ) -> None:
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(
            root, _entry(lifecycle=lifecycle, refs=[])
        )
        entries = lc.load_lifecycle_manifest(manifest)
        findings = lc.evidence_findings(entries, root)
        assert _codes(findings) == {'landed_without_evidence'}
        assert lifecycle in findings[0].message

    def test_nonlanded_with_stale_ref_still_fails(self, tmp_path: Path) -> None:
        """Refs must resolve in ANY state — a stale ref on planned work is
        still drift (global 'every ref resolves' rule)."""
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(
            root, _entry(lifecycle='planned', refs=['docs/issues/gone.md'])
        )
        entries = lc.load_lifecycle_manifest(manifest)
        assert _codes(lc.evidence_findings(entries, root)) == {
            'evidence_ref_unresolvable'
        }


class TestMainIntegration:
    def test_main_fails_closed_and_writes_json(self, tmp_path: Path) -> None:
        root = _fixture_root(tmp_path)
        manifest = _write_manifest(root, _entry(refs=['docs/issues/gone.md']))
        verification = root / 'scripts' / 'issue_verification_manifest.yaml'
        verification.write_text('issues: []\n', encoding='utf-8')
        json_out = root / 'out' / 'report.json'
        rc = lc.main([
            '--lifecycle-manifest', str(manifest),
            '--verification-manifest', str(verification),
            '--json', str(json_out),
            '--report', str(root / 'out' / 'report.md'),
        ])
        assert rc == 1
        report = json.loads(
            io.open(json_out, encoding='utf-8').read()
        )
        codes = {f['code'] for f in report['findings']}
        assert 'evidence_ref_unresolvable' in codes
        assert any(f['issue'] == 1 for f in report['findings'])

    def test_manifest_location_derives_repo_root(self, tmp_path: Path) -> None:
        """Without --repo-root, refs resolve against the manifest's own
        <root>/scripts/ location — not the process cwd."""
        root = _fixture_root(tmp_path)
        (root / 'docs' / 'issues' / 'issue-1-doc.md').write_text('x', encoding='utf-8')
        manifest = _write_manifest(root, _entry(refs=['docs/issues/issue-1-doc.md']))
        verification = root / 'scripts' / 'issue_verification_manifest.yaml'
        verification.write_text('issues: []\n', encoding='utf-8')
        rc = lc.main([
            '--lifecycle-manifest', str(manifest),
            '--verification-manifest', str(verification),
        ])
        assert rc == 0


class TestRealManifest:
    """The checked-in manifest itself must satisfy the policy — this is the
    canary that would have caught the #805/#807 rename drift on main."""

    def test_real_manifest_evidence_all_resolves(self) -> None:
        manifest = REPO_ROOT / 'scripts' / 'issue_lifecycle_manifest.yaml'
        entries = lc.load_lifecycle_manifest(manifest)
        findings = lc.evidence_findings(entries, REPO_ROOT)
        errors = [f for f in findings if f.severity == 'error']
        assert errors == [], (
            'unresolved lifecycle evidence: '
            + '; '.join(f'#{f.issue} {f.code}' for f in errors)
        )
