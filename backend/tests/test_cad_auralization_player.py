"""#784: comparison player — matched A/B switching, provenance, stale."""

from __future__ import annotations

import pytest

from htdt.cad_auralization_player import (
    ListeningSource,
    mark_source_stale,
    open_comparison,
    pause,
    play,
    provenance_badge,
    seek,
    set_loop,
    switch_alternative,
)


def _source(render_ref: str, *, provenance='predicted', readiness='ready'):
    return ListeningSource(
        render_ref=render_ref,
        label=render_ref,
        provenance=provenance,
        seat_ref='seat-1',
        variant_ref='var-a',
        source_program_ref='prog-1',
        duration_s=30.0,
        sample_rate_hz=48_000,
        channel_count=2,
        readiness=readiness,
        detail='' if readiness == 'ready' else f'source {readiness}',
    )


def _session():
    return open_comparison(
        session_id='cmp-1',
        document_id='doc-1',
        source_a=_source('render-a'),
        source_b=_source('render-b', provenance='measured'),
    )


def test_provenance_badges_never_hidden() -> None:
    assert provenance_badge(_source('x')) == 'PREDICTED'
    assert (
        provenance_badge(_source('y', provenance='measured')) == 'MEASURED'
    )


def test_open_requires_ready_sources() -> None:
    with pytest.raises(ValueError, match='needs a render'):
        open_comparison(
            session_id='cmp-x',
            document_id='doc-1',
            source_a=_source('render-a', readiness='render_required'),
        )
    with pytest.raises(ValueError, match='stale'):
        open_comparison(
            session_id='cmp-y',
            document_id='doc-1',
            source_a=_source('render-a', readiness='stale'),
        )


def test_switch_preserves_position_and_loop() -> None:
    session = _session()
    session = play(session)
    session = seek(session, 12.5)
    session = set_loop(session, 10.0, 15.0)
    session = switch_alternative(session)
    assert session.active == 'b'
    assert session.position_s == 12.5
    assert session.loop_start_s == 10.0 and session.loop_end_s == 15.0
    assert session.level_policy == 'lufs_matched'
    # Switch back returns to A at the same program position.
    session = switch_alternative(session)
    assert session.active == 'a' and session.position_s == 12.5


def test_switch_without_b_rejected() -> None:
    session = open_comparison(
        session_id='cmp-1',
        document_id='doc-1',
        source_a=_source('render-a'),
    )
    with pytest.raises(ValueError, match='no B alternative'):
        switch_alternative(session)


def test_stale_active_blocks_session() -> None:
    session = play(_session())
    session = mark_source_stale(
        session, 'render-a', 'variant var-a changed'
    )
    assert session.state == 'blocked'
    assert 'stale' in session.detail


def test_stale_inactive_keeps_playing() -> None:
    session = play(_session())
    session = mark_source_stale(session, 'render-b', 'variant moved')
    assert session.state == 'playing'
    session = switch_alternative(session)
    assert session.active == 'b' and session.state == 'blocked'


def test_seek_bounds_and_pause() -> None:
    session = play(_session())
    with pytest.raises(ValueError, match='outside program'):
        seek(session, 31.0)
    session = pause(session)
    assert session.state == 'paused'
    with pytest.raises(ValueError, match='playing'):
        pause(session)


def test_loop_must_be_inside_program() -> None:
    session = _session()
    with pytest.raises(ValueError, match='inside the program'):
        set_loop(session, 5.0, 40.0)
