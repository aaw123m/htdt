"""REV47 / issue #534 — PresentationSession, A/B bindings, review/proposal
package authorities (PM10–PM40).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.canonical_json import canonical_sha256
from htdt.cad_design_comparison import (
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_presentation_repository import (
    CadPresentationRepository,
    PresentationConflictError,
)
from htdt.cad_presentation_session import (
    PresentationRenderSettings,
    PresentationSection,
    SynchronizedSide,
    build_presentation_proposal,
    build_presentation_session,
    build_sync_binding,
    build_viewpoint,
    synchronized_steps,
)
from htdt.cad_proposal_package import (
    ProposalPackageEntry,
    build_proposal_package,
    verify_proposal_package,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_review_package import (
    ReviewPackageEntry,
    build_review_package,
    verify_review_package,
)
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_view_state import RoomCameraState

NOW = '2026-10-04T00:00:00+00:00'


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _camera(x: float = 3.0) -> RoomCameraState:
    return RoomCameraState(
        position=(x, 2.0, 3.0),
        focal_point=(0.0, 0.0, 0.0),
        view_up=(0.0, 0.0, 1.0),
        projection='perspective',
        standard_view='custom',
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision_a = scene_repository.save(
        _scene(fl_x=1.2), parent_revision_id=None
    ).revision
    revision_b = scene_repository.save(
        _scene(fl_x=1.6), parent_revision_id=revision_a.revision_id
    ).revision
    repository = CadPresentationRepository(scene_repository)
    comparison_repository = CadDesignComparisonRepository(scene_repository)
    return (
        scene_repository,
        revision_a,
        revision_b,
        repository,
        comparison_repository,
    )


def _session(revision, *viewpoints, **overrides) -> object:
    kwargs = dict(
        document_id=revision.document_id,
        label='クライアントレビュー',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        viewpoints=viewpoints,
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    return build_presentation_session(**kwargs)


def _comparison_set(repository, revision_a, revision_b):
    comparison_set = build_comparison_set(
        document_id=revision_a.document_id,
        name='スピーカー候補',
        alternatives=(
            build_alternative(
                label='案A', scene_revision=revision_a, created_at_utc=NOW
            ),
            build_alternative(
                label='案B', scene_revision=revision_b, created_at_utc=NOW
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    return comparison_set


# ---------------------------------------------------------------------------
# PM10 — PresentationSession + viewpoints


def test_viewpoint_is_deterministic() -> None:
    camera = _camera()
    first = build_viewpoint(
        name='スクリーン正面', camera=camera, viewpoint_id='vp-1'
    )
    second = build_viewpoint(
        name='スクリーン正面', camera=camera, viewpoint_id='vp-1'
    )
    assert first.viewpoint_sha256 == second.viewpoint_sha256
    assert first == second
    # Rebuilt with different content → different seal.
    other = build_viewpoint(
        name='別視点', camera=camera, viewpoint_id='vp-1'
    )
    assert other.viewpoint_sha256 != first.viewpoint_sha256


def test_session_deterministic_and_round_trips(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository, _cmp = _fixture(tmp_path)
    viewpoints = (
        build_viewpoint(name='正面', camera=_camera(3.0)),
        build_viewpoint(name='後方', camera=_camera(-3.0)),
    )
    session = _session(
        rev_a,
        *viewpoints,
        sections=(
            PresentationSection(
                section_id='sec-2',
                title='後方から',
                viewpoint_id=viewpoints[1].viewpoint_id,
            ),
            PresentationSection(
                section_id='sec-1',
                title='正面から',
                viewpoint_id=viewpoints[0].viewpoint_id,
            ),
        ),
        status_label='proposed',
        session_id='sess-1',
    )
    again = _session(
        rev_a,
        *viewpoints,
        sections=session.sections,
        status_label='proposed',
        session_id='sess-1',
    )
    assert session.session_sha256 == again.session_sha256
    assert session == again

    ordered = session.ordered_viewpoints()
    assert ordered[0].viewpoint_id == viewpoints[1].viewpoint_id
    assert ordered[1].viewpoint_id == viewpoints[0].viewpoint_id

    repository.save_session(session)
    loaded = repository.get_session(session.session_id)
    assert loaded == session
    assert repository.verify_persisted_session(session.session_id)


def test_session_materializes_exact_scene(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, repository, _cmp = _fixture(tmp_path)
    session = _session(
        rev_a, build_viewpoint(name='正面', camera=_camera())
    )
    repository.save_session(session)
    document = repository.session_document(session)
    entity = document.entity('fl')
    assert entity is not None
    assert entity.position.x_m == pytest.approx(1.2)
    assert repository.session_stale(session) is True
    del rev_b


def test_session_save_fails_closed_on_authority(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository, _cmp = _fixture(tmp_path)
    viewpoint = build_viewpoint(name='正面', camera=_camera())
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_session(
            _session(rev_a, viewpoint, scene_revision_id='rev-missing')
        )


def test_session_rejects_wrong_hash(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository, _cmp = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        scene_content_hash='0' * 64,
    )
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_session(session)


def test_session_rejects_unknown_viewpoint_anchor(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository, _cmp = _fixture(tmp_path)
    viewpoint = build_viewpoint(
        name='存在しない物体', camera=_camera(), focus_entity_id='ghost'
    )
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_session(_session(rev_a, viewpoint))


def test_session_comparison_pin_validated(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, repository, comparison = _fixture(tmp_path)
    comparison_set = _comparison_set(comparison, rev_a, rev_b)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        comparison_set_id=comparison_set.set_id,
        comparison_set_sha256=comparison_set.set_sha256,
    )
    repository.save_session(session)
    assert repository.verify_persisted_session(session.session_id)
    wrong = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        comparison_set_id=comparison_set.set_id,
        comparison_set_sha256='0' * 64,
        session_id='other-session',
    )
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_session(wrong)


# ---------------------------------------------------------------------------
# PM20 — synchronized A/B + proposal records


def test_synchronized_steps_pair_ordinally() -> None:
    left = (
        build_viewpoint(name='l1', camera=_camera()),
        build_viewpoint(name='l2', camera=_camera(4.0)),
    )
    right = (build_viewpoint(name='r1', camera=_camera(5.0)),)
    steps = synchronized_steps(left, right)
    assert len(steps) == 2
    assert steps[0].left_view == 'pinned' and steps[0].right_view == 'pinned'
    assert steps[1].left_view == 'pinned' and steps[1].right_view == 'fit'


def test_proposal_kind_contract() -> None:
    _session_fixture = None  # proposals need a real session; model gate first
    session_kwargs = dict(
        document_id='doc-1',
        label='セッション',
        scene_revision_id='rev',
        scene_content_hash='a' * 64,
        viewpoints=(build_viewpoint(name='v', camera=_camera()),),
        created_at_utc=NOW,
    )
    session = build_presentation_session(**session_kwargs)
    with pytest.raises(ValidationError):
        build_presentation_proposal(
            session=session,
            kind='variant_candidate',
            title='候補',
            created_at_utc=NOW,
        )
    with pytest.raises(ValidationError):
        build_presentation_proposal(
            session=session,
            kind='annotation_only',
            title='候補',
            system_variant_id='var-1',
            system_variant_sha256='b' * 64,
            created_at_utc=NOW,
        )
    ok = build_presentation_proposal(
        session=session,
        kind='annotation_only',
        title='メモ提案',
        created_at_utc=NOW,
    )
    assert ok.kind == 'annotation_only'


def test_proposal_requires_existing_session(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository, _cmp = _fixture(tmp_path)
    session = _session(
        rev_a, build_viewpoint(name='正面', camera=_camera())
    )
    proposal = build_presentation_proposal(
        session=session,
        kind='annotation_only',
        title='未保存セッションへの提案',
        created_at_utc=NOW,
    )
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_proposal(proposal)
    repository.save_session(session)
    repository.save_proposal(proposal)
    assert repository.get_proposal(proposal.proposal_id) == proposal
    assert repository.verify_persisted_proposal(proposal.proposal_id)


def test_sync_binding_round_trip_and_distinctness(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, repository, comparison = _fixture(tmp_path)
    comparison_set = _comparison_set(comparison, rev_a, rev_b)
    alt_a, alt_b = comparison_set.alternatives

    def _side(alternative) -> SynchronizedSide:
        return SynchronizedSide(
            kind='comparison_alternative',
            comparison_set_id=comparison_set.set_id,
            comparison_set_sha256=comparison_set.set_sha256,
            alternative_id=alternative.alternative_id,
            alternative_sha256=alternative.alternative_sha256,
            scene_revision_id=alternative.scene_revision_id,
            scene_content_hash=alternative.scene_content_hash,
            label=alternative.label,
        )

    with pytest.raises(ValidationError):
        build_sync_binding(
            document_id=rev_a.document_id,
            label='同一案同士',
            left=_side(alt_a),
            right=_side(alt_a),
            created_at_utc=NOW,
        )

    binding = build_sync_binding(
        document_id=rev_a.document_id,
        label='案A / 案B',
        left=_side(alt_a),
        right=_side(alt_b),
        lockstep_viewpoint=True,
        created_at_utc=NOW,
    )
    repository.save_binding(binding)
    loaded = repository.get_binding(binding.binding_id)
    assert loaded == binding
    assert repository.verify_persisted_binding(binding.binding_id)


def test_binding_rejects_unknown_alternative(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, repository, comparison = _fixture(tmp_path)
    comparison_set = _comparison_set(comparison, rev_a, rev_b)
    alt_a, alt_b = comparison_set.alternatives
    bad = SynchronizedSide(
        kind='comparison_alternative',
        comparison_set_id=comparison_set.set_id,
        comparison_set_sha256=comparison_set.set_sha256,
        alternative_id=alt_b.alternative_id,
        alternative_sha256='0' * 64,
        scene_revision_id=alt_b.scene_revision_id,
        scene_content_hash=alt_b.scene_content_hash,
        label=alt_b.label,
    )
    good = SynchronizedSide(
        kind='comparison_alternative',
        comparison_set_id=comparison_set.set_id,
        comparison_set_sha256=comparison_set.set_sha256,
        alternative_id=alt_a.alternative_id,
        alternative_sha256=alt_a.alternative_sha256,
        scene_revision_id=alt_a.scene_revision_id,
        scene_content_hash=alt_a.scene_content_hash,
        label=alt_a.label,
    )
    binding = build_sync_binding(
        document_id=rev_a.document_id,
        label='案A / 壊れた案B',
        left=good,
        right=bad,
        created_at_utc=NOW,
    )
    with pytest.raises((ValueError, PresentationConflictError)):
        repository.save_binding(binding)


# ---------------------------------------------------------------------------
# PM30 — offline review package


def _png_stub(document, viewpoint, *, yaw_deg: int) -> bytes:
    return (
        b'PNG-STUB:' + viewpoint.viewpoint_id.encode() + b':' + str(yaw_deg).encode()
    )


class _StubRenderer:
    def renderer_id(self) -> str:
        return 'stub/1.0'

    def render_frame(self, document, viewpoint, *, yaw_deg: int) -> bytes:
        return _png_stub(document, viewpoint, yaw_deg=yaw_deg)


def _saved_session(tmp_path: Path):
    scenes, rev_a, rev_b, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        build_viewpoint(name='後方', camera=_camera(-3.0)),
        render=PresentationRenderSettings(yaw_step_deg=60),
    )
    repository.save_session(session)
    return scenes, repository, comparison, session


def test_review_package_manifest_is_honest(tmp_path: Path) -> None:
    scenes, repository, _comparison, session = _saved_session(tmp_path)
    out = tmp_path / 'review'
    result = build_review_package(
        session, out, scenes, presentation_repository=repository,
        renderer=_StubRenderer(), generated_at_utc=NOW,
        include_drawings=False,
    )
    manifest = result.manifest
    assert manifest.session_sha256 == session.session_sha256
    assert manifest.scene_content_hash == session.scene_content_hash
    states = {row.capability: row.state for row in manifest.capability_rows}
    assert states['perspective_renders'] == 'rendered'
    assert states['equirectangular_panorama'] == 'unavailable'
    assert states['offline_viewer'] == 'rendered'
    render_entries = [
        entry for entry in manifest.entries if entry.kind == 'render'
    ]
    # 2 viewpoints x (1 pinned + 4 yaw offsets)
    assert len(render_entries) == 10
    semantic_entry = next(
        entry for entry in manifest.entries if entry.kind == 'semantic'
    )
    payload = json.loads((out / semantic_entry.path).read_text('utf-8'))
    # The sealed hash is computed over the semantic payload — verify it.
    assert canonical_sha256(payload) == session.session_sha256
    assert payload['scene_revision_id'] == session.scene_revision_id
    assert 'C:\\' not in json.dumps(manifest.model_dump(mode='json'))
    assert not result.warnings
    verify_review_package(out)


def test_review_package_detects_tamper(tmp_path: Path) -> None:
    scenes, repository, _comparison, session = _saved_session(tmp_path)
    out = tmp_path / 'review'
    build_review_package(
        session, out, scenes, presentation_repository=repository,
        renderer=_StubRenderer(), generated_at_utc=NOW,
        include_drawings=False,
    )
    renders = list((out / 'renders').glob('*.png'))
    assert renders
    renders[0].write_bytes(b'corrupted')
    with pytest.raises(ValueError):
        verify_review_package(out)


def test_review_package_declares_unavailable_render(tmp_path: Path) -> None:
    scenes, repository, _comparison, session = _saved_session(tmp_path)
    out = tmp_path / 'review'
    result = build_review_package(
        session, out, scenes, presentation_repository=repository,
        renderer=None, generated_at_utc=NOW,
        include_drawings=False,
    )
    states = {row.capability: row.state for row in result.manifest.capability_rows}
    assert states['perspective_renders'] == 'unavailable'
    assert states['yaw_stepped_renders'] == 'unavailable'
    assert not list((out / 'renders').glob('*.png'))
    verify_review_package(out)


def test_review_package_entry_rejects_bad_paths() -> None:
    for bad in ('/abs.png', 'C:/win.png', '../up.png', 'a\\b.png', 'a:b.png'):
        with pytest.raises(ValidationError):
            ReviewPackageEntry(
                path=bad, sha256='0' * 64, byte_length=1, kind='render'
            )
    ok = ReviewPackageEntry(
        path='renders/01.png', sha256='0' * 64, byte_length=1, kind='render'
    )
    assert ok.path == 'renders/01.png'


# ---------------------------------------------------------------------------
# PM40 — proposal package


def test_proposal_package_builds_and_verifies(tmp_path: Path) -> None:
    scenes, rev_a, rev_b, repository, comparison = _fixture(tmp_path)
    comparison_set = _comparison_set(comparison, rev_a, rev_b)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        comparison_set_id=comparison_set.set_id,
        comparison_set_sha256=comparison_set.set_sha256,
    )
    repository.save_session(session)
    out = tmp_path / 'proposal'
    result = build_proposal_package(
        session, out, scenes,
        presentation_repository=repository,
        comparison_repository=comparison,
        generated_at_utc=NOW,
    )
    manifest = result.manifest
    assert manifest.session_sha256 == session.session_sha256
    assert manifest.installation_output_sha256
    html = (out / 'proposal.html').read_text('utf-8')
    assert 'クライアントレビュー' in html
    assert '案A' in html  # comparison alternative listed
    assert 'C:\\' not in html
    verify_proposal_package(out)


def test_proposal_package_rejects_bad_paths() -> None:
    with pytest.raises(ValidationError):
        ProposalPackageEntry(
            path='../escape.html',
            sha256='0' * 64,
            byte_length=1,
            kind='document',
        )
