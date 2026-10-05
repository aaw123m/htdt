"""Append-only persistence for the immersive render-path authority
(#603).

Six tables:

* ``cad_immersive_contents`` — sealed content-metadata profiles.
* ``cad_renderer_capabilities`` — sealed device+firmware+license
  capability profiles.
* ``cad_speaker_layouts`` — sealed layout declarations; the four
  layout kinds (physically_installed / processor_configured /
  content_native / rendered_output) stay distinct authorities.
* ``cad_render_sessions`` — sealed bound playback states; a session
  may only be persisted against a stored content profile whose sha
  matches ``content_ref``.
* ``cad_render_output_observations`` — sealed output-activity
  observations bound to a stored session.
* ``cad_render_path_qualifications`` — sealed per-session verdicts
  produced by ``evaluate_render_path``.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_render_path import (
    ImmersiveContentProfile,
    RenderedOutputObservation,
    RenderPathQualification,
    RenderSession,
    RendererCapabilityProfile,
    SpeakerLayoutDeclaration,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class RenderPathConflictError(ValueError):
    """A render-path save violated append-only identity rules."""


class RenderPathIntegrityError(ValueError):
    """A stored render-path row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise RenderPathIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise RenderPathIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadRenderPathRepository:
    """Native storage for the #603 render-path authority records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_immersive_contents',
                'cad_renderer_capabilities',
                'cad_speaker_layouts',
                'cad_render_sessions',
                'cad_render_output_observations',
                'cad_render_path_qualifications',
            )

    # ------------------------------------------------------------------
    # Shared helpers

    def _get_payload(self, table: str, column: str, key: str, model):
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} '
                f'WHERE {column}=?',
                (key,),
            ).fetchone()
        if row is None:
            return None
        return model.model_validate_json(row['payload_json'])

    def _list(
        self,
        *,
        table: str,
        model,
        where: str = 'document_id=?',
        params: tuple[object, ...] = (),
        order: str = 'seq ASC',
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                f'ORDER BY {order}',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Immersive content profiles

    def save_content(self, content: ImmersiveContentProfile) -> None:
        _assert_sealed(content, 'content_sha256', 'content_id')
        existing = self.get_content(content.content_id)
        if existing is not None:
            if existing.content_sha256 == content.content_sha256:
                return
            raise RenderPathConflictError(
                'immersive content profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_immersive_contents (
                    content_id, content_sha256, document_id, label,
                    metadata_class, format_label, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content.content_id,
                    content.content_sha256,
                    content.document_id,
                    content.label,
                    content.metadata_class,
                    content.format_label,
                    content.declared_at_utc,
                    content.model_dump_json(),
                ),
            )

    def get_content(
        self, content_id: str
    ) -> ImmersiveContentProfile | None:
        return self._get_payload(
            'cad_immersive_contents', 'content_id', content_id,
            ImmersiveContentProfile,
        )

    def list_contents(
        self, document_id: str
    ) -> tuple[ImmersiveContentProfile, ...]:
        return self._list(
            table='cad_immersive_contents',
            model=ImmersiveContentProfile,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Renderer capability profiles

    def save_capability(
        self, capability: RendererCapabilityProfile
    ) -> None:
        _assert_sealed(
            capability, 'capability_sha256', 'capability_id'
        )
        existing = self.get_capability(capability.capability_id)
        if existing is not None:
            if existing.capability_sha256 == capability.capability_sha256:
                return
            raise RenderPathConflictError(
                'renderer capability profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_renderer_capabilities (
                    capability_id, capability_sha256, document_id,
                    model_label, capability_source, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.capability_sha256,
                    capability.document_id,
                    capability.model_label,
                    capability.capability_source,
                    capability.declared_at_utc,
                    capability.model_dump_json(),
                ),
            )

    def get_capability(
        self, capability_id: str
    ) -> RendererCapabilityProfile | None:
        return self._get_payload(
            'cad_renderer_capabilities', 'capability_id', capability_id,
            RendererCapabilityProfile,
        )

    def list_capabilities(
        self, document_id: str
    ) -> tuple[RendererCapabilityProfile, ...]:
        return self._list(
            table='cad_renderer_capabilities',
            model=RendererCapabilityProfile,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Speaker layout declarations

    def save_layout(self, layout: SpeakerLayoutDeclaration) -> None:
        _assert_sealed(layout, 'layout_sha256', 'layout_id')
        existing = self.get_layout(layout.layout_id)
        if existing is not None:
            if existing.layout_sha256 == layout.layout_sha256:
                return
            raise RenderPathConflictError(
                'layout declarations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_speaker_layouts (
                    layout_id, layout_sha256, document_id, kind, label,
                    evidence, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    layout.layout_id,
                    layout.layout_sha256,
                    layout.document_id,
                    layout.kind,
                    layout.label,
                    layout.evidence,
                    layout.declared_at_utc,
                    layout.model_dump_json(),
                ),
            )

    def get_layout(
        self, layout_id: str
    ) -> SpeakerLayoutDeclaration | None:
        return self._get_payload(
            'cad_speaker_layouts', 'layout_id', layout_id,
            SpeakerLayoutDeclaration,
        )

    def list_layouts(
        self, document_id: str
    ) -> tuple[SpeakerLayoutDeclaration, ...]:
        return self._list(
            table='cad_speaker_layouts',
            model=SpeakerLayoutDeclaration,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Render sessions

    def save_session(self, session: RenderSession) -> None:
        _assert_sealed(session, 'session_sha256', 'session_id')
        existing = self.get_session(session.session_id)
        if existing is not None:
            if existing.session_sha256 == session.session_sha256:
                return
            raise RenderPathConflictError(
                'render sessions are append-only'
            )
        content = self.get_content(session.content_ref.ref_id)
        if content is None:
            raise RenderPathIntegrityError(
                'a render session must reference a persisted content '
                'profile'
            )
        if content.content_sha256 != session.content_ref.ref_sha256:
            raise RenderPathIntegrityError(
                'render session content hash does not match the stored '
                'content profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_render_sessions (
                    session_id, session_sha256, document_id,
                    content_ref_id, decoder_mode, upmixer_state,
                    started_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.session_sha256,
                    session.document_id,
                    session.content_ref.ref_id,
                    session.decoder_mode,
                    session.upmixer_state,
                    session.started_at_utc,
                    session.model_dump_json(),
                ),
            )

    def get_session(self, session_id: str) -> RenderSession | None:
        return self._get_payload(
            'cad_render_sessions', 'session_id', session_id, RenderSession
        )

    def list_sessions(
        self, document_id: str
    ) -> tuple[RenderSession, ...]:
        return self._list(
            table='cad_render_sessions',
            model=RenderSession,
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Rendered output observations

    def save_observation(
        self, observation: RenderedOutputObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == (
                observation.observation_sha256
            ):
                return
            raise RenderPathConflictError(
                'output observations are append-only'
            )
        session = self.get_session(observation.session_ref.ref_id)
        if session is None:
            raise RenderPathIntegrityError(
                'an output observation must reference a persisted '
                'render session'
            )
        if session.session_sha256 != observation.session_ref.ref_sha256:
            raise RenderPathIntegrityError(
                'output observation session hash does not match the '
                'stored session'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_render_output_observations (
                    observation_id, observation_sha256, document_id,
                    session_ref_id, capture_method, observed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.session_ref.ref_id,
                    observation.capture_method,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> RenderedOutputObservation | None:
        return self._get_payload(
            'cad_render_output_observations', 'observation_id',
            observation_id, RenderedOutputObservation,
        )

    def list_observations(
        self, document_id: str
    ) -> tuple[RenderedOutputObservation, ...]:
        return self._list(
            table='cad_render_output_observations',
            model=RenderedOutputObservation,
            params=(document_id,),
        )

    def observations_for_session(
        self, session_id: str
    ) -> tuple[RenderedOutputObservation, ...]:
        return self._list(
            table='cad_render_output_observations',
            model=RenderedOutputObservation,
            where='session_ref_id=?',
            params=(session_id,),
        )

    # ------------------------------------------------------------------
    # Render-path qualifications

    def save_qualification(
        self, qualification: RenderPathQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == (
                qualification.qualification_sha256
            ):
                return
            raise RenderPathConflictError(
                'render-path qualifications are append-only'
            )
        session = self.get_session(qualification.session_ref.ref_id)
        if session is None:
            raise RenderPathIntegrityError(
                'a render-path qualification must reference a '
                'persisted session'
            )
        if session.session_sha256 != (
            qualification.session_ref.ref_sha256
        ):
            raise RenderPathIntegrityError(
                'qualification session hash does not match the stored '
                'session'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_render_path_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    session_ref_id, overall_state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.session_ref.ref_id,
                    qualification.overall_state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> RenderPathQualification | None:
        return self._get_payload(
            'cad_render_path_qualifications', 'qualification_id',
            qualification_id, RenderPathQualification,
        )

    def list_qualifications(
        self, document_id: str
    ) -> tuple[RenderPathQualification, ...]:
        return self._list(
            table='cad_render_path_qualifications',
            model=RenderPathQualification,
            params=(document_id,),
        )
