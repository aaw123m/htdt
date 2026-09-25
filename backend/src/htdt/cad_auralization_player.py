"""Auralization comparison player backend (#784).

#515 owns the render authority; #518 owns blind A/B semantics. This module
owns the comparison-session state machine behind the listening surface:
select A/B, matched program position on switch, loop region, level
policy, provenance badges, readiness/stale state — all Qt-free so the
player logic is testable headless and identical to what the UI binds.

Hard rules:
- A/B switching preserves excerpt, timestamp, channel routing, output
  format and gain policy — never restarts the program;
- predicted and measured renders always carry their provenance badge;
- a stale or not-yet-rendered source is never presented as listenable;
- the player produces listening state, not a second render authority.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LevelPolicy = Literal['unity', 'lufs_matched', 'peak_matched']
ProvenanceKind = Literal['predicted', 'measured', 'hybrid']
ListenReadiness = Literal[
    'ready', 'render_required', 'stale', 'unavailable'
]
PlayerState = Literal['idle', 'playing', 'paused', 'blocked']
ActiveAlternative = Literal['a', 'b']


class ListeningSource(BaseModel):
    """One bound render alternative — an exact authority reference, not a
    file path the user managed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    render_ref: str = Field(min_length=1)
    label: str = Field(min_length=1)
    provenance: ProvenanceKind
    seat_ref: str = Field(min_length=1)
    variant_ref: str | None = None
    source_program_ref: str = Field(min_length=1)
    duration_s: float = Field(gt=0.0)
    sample_rate_hz: int = Field(gt=0)
    channel_count: int = Field(ge=1)
    readiness: ListenReadiness = 'ready'
    detail: str = ''

    @model_validator(mode='after')
    def valid_source(self) -> 'ListeningSource':
        if self.readiness != 'ready' and not self.detail:
            raise ValueError(
                'a non-ready source must explain its state'
            )
        return self


def provenance_badge(source: ListeningSource) -> str:
    """The badge text shown beside the source — predicted vs measured is
    never hidden."""
    return {
        'predicted': 'PREDICTED',
        'measured': 'MEASURED',
        'hybrid': 'PREDICTED+MEASURED',
    }[source.provenance]


class ComparisonSession(BaseModel):
    """Mutable player state; every transition returns a new instance."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    session_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_a: ListeningSource
    source_b: ListeningSource | None = None
    active: ActiveAlternative = 'a'
    position_s: float = Field(ge=0.0)
    state: PlayerState = 'idle'
    level_policy: LevelPolicy = 'lufs_matched'
    loop_start_s: float | None = None
    loop_end_s: float | None = None
    detail: str = ''

    @model_validator(mode='after')
    def valid_session(self) -> 'ComparisonSession':
        sources = [self.source_a] + (
            [self.source_b] if self.source_b is not None else []
        )
        for source in sources:
            if source.readiness == 'unavailable':
                raise ValueError(
                    f'source {source.render_ref} is unavailable — a '
                    'comparison session cannot bind it'
                )
        active = (
            self.source_a if self.active == 'a' else self.source_b
        )
        if self.state in ('playing', 'paused'):
            if active is None or active.readiness != 'ready':
                raise ValueError(
                    'a session may only play a ready source'
                )
        duration = max(s.duration_s for s in sources)
        if self.position_s > duration:
            raise ValueError('position exceeds program duration')
        if (self.loop_start_s is None) != (self.loop_end_s is None):
            raise ValueError('loop requires both bounds')
        if self.loop_start_s is not None:
            assert self.loop_end_s is not None
            if not (0.0 <= self.loop_start_s < self.loop_end_s <= duration):
                raise ValueError('loop region must sit inside the program')
        return self

    def active_source(self) -> ListeningSource:
        if self.active == 'a':
            return self.source_a
        assert self.source_b is not None
        return self.source_b


def open_comparison(
    *,
    session_id: str,
    document_id: str,
    source_a: ListeningSource,
    source_b: ListeningSource | None = None,
    level_policy: LevelPolicy = 'lufs_matched',
) -> ComparisonSession:
    """Open a comparison; readiness states are checked up front."""
    for source in (source_a, source_b):
        if source is None:
            continue
        if source.readiness == 'render_required':
            raise ValueError(
                f'source {source.render_ref} needs a render before the '
                'comparison can open'
            )
        if source.readiness == 'stale':
            raise ValueError(
                f'source {source.render_ref} is stale — re-render or '
                'acknowledge before listening'
            )
    return ComparisonSession(
        session_id=session_id,
        document_id=document_id,
        source_a=source_a,
        source_b=source_b,
        level_policy=level_policy,
        position_s=0.0,
        state='idle',
    )


def _copy(session: ComparisonSession, **updates: Any) -> ComparisonSession:
    payload = session.model_dump(mode='python')
    payload.update(updates)
    return ComparisonSession.model_validate(payload)


def play(session: ComparisonSession) -> ComparisonSession:
    if session.active_source().readiness != 'ready':
        raise ValueError('active source is not listenable')
    return _copy(session, state='playing')


def pause(session: ComparisonSession) -> ComparisonSession:
    if session.state != 'playing':
        raise ValueError('only a playing session pauses')
    return _copy(session, state='paused')


def seek(session: ComparisonSession, position_s: float) -> ComparisonSession:
    duration = session.active_source().duration_s
    if not (0.0 <= position_s <= duration):
        raise ValueError('seek position outside program duration')
    return _copy(session, position_s=position_s)


def set_loop(
    session: ComparisonSession,
    start_s: float | None,
    end_s: float | None,
) -> ComparisonSession:
    return _copy(session, loop_start_s=start_s, loop_end_s=end_s)


def switch_alternative(session: ComparisonSession) -> ComparisonSession:
    """Instant A/B switch: preserves position, loop, routing and gain
    policy — the switch never restarts or re-levels the program."""
    if session.source_b is None:
        raise ValueError('no B alternative bound')
    target: ActiveAlternative = 'b' if session.active == 'a' else 'a'
    target_source = (
        session.source_b if target == 'b' else session.source_a
    )
    if target_source.readiness != 'ready':
        return _copy(
            session,
            active=target,
            state='blocked',
            detail=(
                f'alternative {target} is {target_source.readiness} — '
                'cannot switch to it'
            ),
        )
    state = session.state
    if state == 'blocked':
        state = 'idle'
    return _copy(session, active=target, state=state, detail='')


def mark_source_stale(
    session: ComparisonSession,
    render_ref: str,
    reason: str,
) -> ComparisonSession:
    """A dependency moved: flip the source to stale; if it was the active
    side the session blocks rather than playing stale audio."""
    def _stale(source: ListeningSource) -> ListeningSource:
        return source.model_copy(
            update={'readiness': 'stale', 'detail': reason}
        )

    updates: dict[str, Any] = {}
    if session.source_a.render_ref == render_ref:
        updates['source_a'] = _stale(session.source_a)
    elif session.source_b is not None and (
        session.source_b.render_ref == render_ref
    ):
        updates['source_b'] = _stale(session.source_b)
    else:
        return session
    stale_active = (
        (updates.get('source_a') is not None and session.active == 'a')
        or (updates.get('source_b') is not None and session.active == 'b')
    )
    if stale_active:
        updates['state'] = 'blocked'
        updates['detail'] = f'active source went stale: {reason}'
    return _copy(session, **updates)


__all__ = [
    'ActiveAlternative',
    'ComparisonSession',
    'LevelPolicy',
    'ListenReadiness',
    'ListeningSource',
    'PlayerState',
    'ProvenanceKind',
    'mark_source_stale',
    'open_comparison',
    'pause',
    'play',
    'provenance_badge',
    'seek',
    'set_loop',
    'switch_alternative',
]
