from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import check_native_schema_compatibility
from .cad_standards import (
    StandardsEvaluation,
    StandardsProfile,
    StandardsSourceAuthority,
    evaluate_standards_profile,
    validate_criterion_source_authority,
)
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .r120_geometry_compiler import ExactExternalAuthorityRef


SourceAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    StandardsSourceAuthority | None,
]


class CadStandardsRepository:
    """Append-only standards profiles/evaluations bound to existing scene authorities.

    Published criterion sources are exact retained authorities: every criterion
    carrying ``source.authority_ref`` is re-resolved at the persistence boundary
    and must reproduce the retained extraction exactly. ``published`` profiles
    additionally require that binding on every criterion; an opaque citation
    string alone is not treated as proof of source provenance.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository | None = None,
        *,
        source_authority_resolver: SourceAuthorityResolver | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.source_authority_resolver = source_authority_resolver
        self.path = Path(scene_repository.path)
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        check_native_schema_compatibility(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_standards_source_authorities (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    authority_id TEXT NOT NULL UNIQUE,
                    authority_version TEXT NOT NULL,
                    semantic_hash_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cad_standards_profiles (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    profile_semantic_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    UNIQUE(profile_id, profile_version)
                );
                CREATE INDEX IF NOT EXISTS idx_standards_profile_id_seq
                    ON cad_standards_profiles(profile_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_standards_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    system_variant_id TEXT,
                    profile_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    profile_semantic_hash TEXT NOT NULL,
                    evaluation_sha256 TEXT NOT NULL UNIQUE,
                    reevaluation_of_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(profile_id, profile_version)
                        REFERENCES cad_standards_profiles(profile_id, profile_version),
                    FOREIGN KEY(reevaluation_of_id)
                        REFERENCES cad_standards_evaluations(evaluation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_standards_evaluation_document_seq
                    ON cad_standards_evaluations(document_id, seq ASC);
                CREATE INDEX IF NOT EXISTS idx_standards_evaluation_scene_seq
                    ON cad_standards_evaluations(scene_revision_id, seq ASC);
                """
            )

    def save_source_authority(
        self,
        authority: StandardsSourceAuthority,
    ) -> StandardsSourceAuthority:
        """Retain an exact source-document authority for audit/re-resolution."""

        authority = StandardsSourceAuthority.model_validate(
            authority.model_dump(mode='python')
        )
        existing = self.get_source_authority(authority.authority_id)
        if existing is not None:
            if existing.semantic_hash_sha256 != authority.semantic_hash_sha256:
                raise ValueError(
                    'StandardsSourceAuthority id exists with different semantics'
                )
            return existing
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standards_source_authorities(
                    authority_id, authority_version,
                    semantic_hash_sha256, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    authority.authority_id,
                    authority.authority_version,
                    authority.semantic_hash_sha256,
                    authority.model_dump_json(),
                ),
            )
        return authority

    def get_source_authority(
        self,
        authority_id: str,
    ) -> StandardsSourceAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_standards_source_authorities '
                'WHERE authority_id=?',
                (authority_id,),
            ).fetchone()
        if row is None:
            return None
        return StandardsSourceAuthority.model_validate_json(row['payload_json'])

    def list_source_authorities(self) -> tuple[StandardsSourceAuthority, ...]:
        """Return every retained source authority in insertion order."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_standards_source_authorities '
                'ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            StandardsSourceAuthority.model_validate_json(row['payload_json'])
            for row in rows
        )

    def resolve_source_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> StandardsSourceAuthority | None:
        """Resolve a typed ref against the retained store with exact identity."""

        authority = self.get_source_authority(ref.authority_id)
        if (
            authority is None
            or authority.authority_version != ref.authority_version
            or authority.semantic_hash_sha256 != ref.semantic_hash_sha256
        ):
            return None
        return authority

    def _resolve_source_authority(
        self,
        ref: ExactExternalAuthorityRef,
        *,
        criterion_id: str,
    ) -> StandardsSourceAuthority:
        resolved = None
        if self.source_authority_resolver is not None:
            candidate = self.source_authority_resolver(ref)
            if candidate is not None:
                resolved = StandardsSourceAuthority.model_validate(
                    candidate.model_dump(mode='python')
                )
        if resolved is None:
            resolved = self.get_source_authority(ref.authority_id)
        if resolved is None:
            raise ValueError(
                f'criterion {criterion_id} source authority does not exist'
            )
        if (
            resolved.authority_id != ref.authority_id
            or resolved.authority_version != ref.authority_version
            or resolved.semantic_hash_sha256 != ref.semantic_hash_sha256
        ):
            raise ValueError(
                f'criterion {criterion_id} source authority mismatch'
            )
        # Pin the resolved authority so historical profile versions remain
        # auditable after the external resolver changes or disappears.
        self.save_source_authority(resolved)
        return resolved

    def _validate_profile_provenance(
        self,
        profile: StandardsProfile,
    ) -> StandardsProfile:
        for criterion in profile.criteria:
            source = criterion.source
            if profile.profile_kind == 'published' and (
                source.authority_ref is None
                or source.extraction_id is None
                or source.content_kind is None
            ):
                raise ValueError(
                    f'published criterion {criterion.criterion_id} requires an '
                    'exact source authority, extraction identity, and explicit '
                    'content kind'
                )
            if source.authority_ref is None:
                continue
            authority = self._resolve_source_authority(
                source.authority_ref,
                criterion_id=criterion.criterion_id,
            )
            try:
                validate_criterion_source_authority(criterion, authority)
            except ValueError as exc:
                raise ValueError(
                    f'criterion {criterion.criterion_id} source provenance '
                    f'rejected: {exc}'
                ) from exc
        return profile

    def save_profile(self, profile: StandardsProfile) -> StandardsProfile:
        profile = StandardsProfile.model_validate(profile.model_dump(mode='python'))
        existing = self.get_profile(profile.profile_id, profile.version)
        if existing is not None:
            if existing.profile_semantic_hash != profile.profile_semantic_hash:
                raise ValueError('StandardsProfile version is immutable')
            return existing

        self._validate_profile_provenance(profile)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standards_profiles(
                    profile_id, profile_version, profile_semantic_hash, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.profile_semantic_hash,
                    profile.model_dump_json(),
                ),
            )
        return profile

    def get_profile(
        self,
        profile_id: str,
        version: str,
    ) -> StandardsProfile | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_standards_profiles '
                'WHERE profile_id=? AND profile_version=?',
                (profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return StandardsProfile.model_validate_json(row['payload_json'])

    def list_profile_versions(self, profile_id: str) -> tuple[StandardsProfile, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_standards_profiles '
                'WHERE profile_id=? ORDER BY seq ASC',
                (profile_id,),
            ).fetchall()
        return tuple(
            StandardsProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_profiles(self) -> tuple[StandardsProfile, ...]:
        """Return every persisted immutable profile version in insertion order."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_standards_profiles ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            StandardsProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_evaluation(
        self,
        evaluation: StandardsEvaluation,
    ) -> StandardsEvaluation:
        evaluation = StandardsEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        profile = self.get_profile(
            evaluation.profile_id,
            evaluation.profile_version,
        )
        if profile is None:
            raise ValueError('standards evaluation references an unsaved StandardsProfile')
        if profile.profile_semantic_hash != evaluation.profile_semantic_hash:
            raise ValueError('standards evaluation profile semantic hash mismatch')

        regenerated = evaluate_standards_profile(
            profile=profile,
            target=evaluation.target,
            observations=evaluation.observations,
            created_at_utc=evaluation.created_at_utc,
            reevaluation_of_id=evaluation.reevaluation_of_id,
        )
        if regenerated != evaluation:
            raise ValueError('standards evaluation does not match evaluator authority')

        revision = self.scene_repository.get(evaluation.target.scene_revision_id)
        if revision is None:
            raise ValueError('standards evaluation SceneRevision does not exist')
        if (
            revision.document_id != evaluation.target.document_id
            or revision.content_hash != evaluation.target.scene_content_hash
        ):
            raise ValueError('standards evaluation SceneRevision authority mismatch')

        target_document = revision.document
        if evaluation.target.system_variant_id is not None:
            if self.system_variant_repository is None:
                raise ValueError(
                    'SystemVariant-bound standards evaluation requires '
                    'CadSystemVariantRepository'
                )
            variant = self.system_variant_repository.get_variant(
                evaluation.target.system_variant_id
            )
            if variant is None:
                raise ValueError('standards evaluation SystemVariant does not exist')
            if (
                variant.variant_sha256 != evaluation.target.system_variant_sha256
                or variant.document_id != revision.document_id
                or variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError('standards evaluation SystemVariant authority mismatch')
            target_document = materialize_system_variant(revision, variant)

        entity_ids = {entity.entity_id for entity in target_document.entities}
        missing_entities = set(evaluation.target.entity_ids) - entity_ids
        if missing_entities:
            raise ValueError(
                'standards evaluation references missing target entities: '
                f'{sorted(missing_entities)}'
            )

        if evaluation.reevaluation_of_id is not None:
            previous = self.get_evaluation(evaluation.reevaluation_of_id)
            if previous is None:
                raise ValueError('standards reevaluation references missing historical evaluation')
            if previous.target != evaluation.target:
                raise ValueError('standards reevaluation target must match historical evaluation')

        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 != evaluation.evaluation_sha256:
                raise ValueError('standards evaluation identity collision')
            return existing

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standards_evaluations(
                    evaluation_id, document_id, scene_revision_id,
                    system_variant_id, profile_id, profile_version,
                    profile_semantic_hash, evaluation_sha256,
                    reevaluation_of_id, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.target.document_id,
                    evaluation.target.scene_revision_id,
                    evaluation.target.system_variant_id,
                    evaluation.profile_id,
                    evaluation.profile_version,
                    evaluation.profile_semantic_hash,
                    evaluation.evaluation_sha256,
                    evaluation.reevaluation_of_id,
                    evaluation.model_dump_json(),
                    evaluation.created_at_utc,
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> StandardsEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_standards_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return StandardsEvaluation.model_validate_json(row['payload_json'])

    def list_evaluations_for_scene(
        self,
        scene_revision_id: str,
    ) -> tuple[StandardsEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_standards_evaluations '
                'WHERE scene_revision_id=? ORDER BY seq ASC',
                (scene_revision_id,),
            ).fetchall()
        return tuple(
            StandardsEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
