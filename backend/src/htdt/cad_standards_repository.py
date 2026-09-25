from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument
from .cad_schema import (
    check_native_schema_compatibility,
    require_native_tables,
)
from .cad_standards import (
    StandardsEvaluation,
    StandardsProfile,
    StandardsSourceAuthority,
    evaluate_standards_profile,
    validate_criterion_source_authority,
)
from .cad_standards_evidence import (
    STANDARDS_EVIDENCE_RESOLVERS,
    CriterionEvidenceContext,
    CriterionEvidenceResolver,
    StandardsObservationAuthority,
    resolve_criterion_observation_evidence,
    validate_observation_evidence,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
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

    Criterion observations are never caller authority: every
    ``CriterionEvidenceRef`` re-resolves against the resolver registered for its
    ``kind`` at save and on every authoritative read. The resolved evidence must
    prove it belongs to the exact SceneRevision/SystemVariant/entities of the
    evaluation target, and the observation must match the canonical projection
    the evidence attests before ``evaluate_standards_profile`` is replayed.
    ``get_evaluation``/``list_evaluations_for_scene`` return only evaluations
    that still reproduce canonically; divergence fails closed.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository | None = None,
        *,
        source_authority_resolver: SourceAuthorityResolver | None = None,
        evidence_resolvers: Mapping[str, CriterionEvidenceResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.source_authority_resolver = source_authority_resolver
        self.evidence_resolvers: dict[str, CriterionEvidenceResolver] = {
            **STANDARDS_EVIDENCE_RESOLVERS,
            **dict(evidence_resolvers or {}),
        }
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
            require_native_tables(connection, 'cad_standards_source_authorities', 'cad_standards_profiles', 'cad_standards_observation_authorities', 'cad_standards_evaluations')

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

    def _resolve_scene_binding(
        self,
        *,
        subject: str,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        system_variant_id: str | None,
        system_variant_sha256: str | None,
    ) -> tuple[SceneRevision, SceneDocument, SystemVariant | None]:
        """Resolve the exact SceneRevision/SystemVariant a claim is bound to.

        Returns the retained revision, the exact target document (the baseline
        scene or the materialized SystemVariant), and the resolved variant.
        Any identity or hash divergence fails closed.
        """

        revision = self.scene_repository.get(scene_revision_id)
        if revision is None:
            raise ValueError(f'{subject} SceneRevision does not exist')
        if (
            revision.document_id != document_id
            or revision.content_hash != scene_content_hash
        ):
            raise ValueError(f'{subject} SceneRevision authority mismatch')

        document = revision.document
        variant = None
        if system_variant_id is not None:
            if self.system_variant_repository is None:
                raise ValueError(
                    f'SystemVariant-bound {subject} requires '
                    'CadSystemVariantRepository'
                )
            variant = self.system_variant_repository.get_variant(
                system_variant_id
            )
            if variant is None:
                raise ValueError(f'{subject} SystemVariant does not exist')
            if (
                variant.variant_sha256 != system_variant_sha256
                or variant.document_id != revision.document_id
                or variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError(f'{subject} SystemVariant authority mismatch')
            document = materialize_system_variant(revision, variant)
        return revision, document, variant

    def save_observation_authority(
        self,
        authority: StandardsObservationAuthority,
    ) -> StandardsObservationAuthority:
        """Retain one exact manual-observation authority for evidence resolution.

        The claimed SceneRevision/SystemVariant/entity binding is re-resolved
        against retained scene authority before the record is admitted, so a
        retained authority always names an exact, existing target scope.
        """

        authority = StandardsObservationAuthority.model_validate(
            authority.model_dump(mode='python')
        )
        # Binding resolution reads through nested repository connections, so it
        # must finish BEFORE the write transaction begins: opening them while
        # BEGIN IMMEDIATE is held can deadlock the write.
        _revision, document, _variant = self._resolve_scene_binding(
            subject='standards observation authority',
            document_id=authority.document_id,
            scene_revision_id=authority.scene_revision_id,
            scene_content_hash=authority.scene_content_hash,
            system_variant_id=authority.system_variant_id,
            system_variant_sha256=authority.system_variant_sha256,
        )
        entity_ids = {entity.entity_id for entity in document.entities}
        missing_entities = set(authority.entity_ids) - entity_ids
        if missing_entities:
            raise ValueError(
                'standards observation authority references missing target '
                f'entities: {sorted(missing_entities)}'
            )

        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate
            # recheck and the insert.
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_standards_observation_authorities
                WHERE authority_id=?
                """,
                (authority.authority_id,),
            ).fetchone()
            if row is not None:
                persisted = StandardsObservationAuthority.model_validate_json(
                    row['payload_json']
                )
                if persisted != authority:
                    raise ValueError(
                        'StandardsObservationAuthority id exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_standards_observation_authorities(
                    authority_id, semantic_hash_sha256, document_id,
                    scene_revision_id, system_variant_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    authority.authority_id,
                    authority.semantic_hash_sha256,
                    authority.document_id,
                    authority.scene_revision_id,
                    authority.system_variant_id,
                    authority.model_dump_json(),
                ),
            )
        return authority

    def get_observation_authority(
        self,
        authority_id: str,
    ) -> StandardsObservationAuthority | None:
        """Return the retained authority; the self-hashed model revalidates."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_standards_observation_authorities
                WHERE authority_id=?
                """,
                (authority_id,),
            ).fetchone()
        if row is None:
            return None
        return StandardsObservationAuthority.model_validate_json(
            row['payload_json']
        )

    def list_observation_authorities(
        self,
    ) -> tuple[StandardsObservationAuthority, ...]:
        """Return every retained observation authority in insertion order."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json '
                'FROM cad_standards_observation_authorities ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            StandardsObservationAuthority.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

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

    def _resolve_evaluation_evidence(
        self,
        evaluation: StandardsEvaluation,
        profile: StandardsProfile,
        *,
        scene_revision: SceneRevision,
        document: SceneDocument,
        system_variant: SystemVariant | None,
    ) -> None:
        """Re-resolve every criterion-observation evidence ref exactly.

        Each ref resolves through the registered resolver for its ``kind``;
        resolved evidence must prove the exact SceneRevision/SystemVariant/
        entity binding, and the claimed observed value/unit/basis/inputs/
        capabilities must match the canonical projection the evidence attests.
        """

        criterion_by_id = {
            criterion.criterion_id: criterion for criterion in profile.criteria
        }
        for observation in evaluation.observations:
            criterion = criterion_by_id.get(observation.criterion_id)
            if criterion is None:
                # evaluate_standards_profile reports observations bound to a
                # criterion outside the profile with its own canonical error.
                continue
            context = CriterionEvidenceContext(
                evaluation=evaluation,
                criterion=criterion,
                observation=observation,
                scene_revision=scene_revision,
                document=document,
                system_variant=system_variant,
                scene_repository=self.scene_repository,
                standards_repository=self,
            )
            resolved = resolve_criterion_observation_evidence(
                context,
                self.evidence_resolvers,
            )
            validate_observation_evidence(observation, resolved)

    def _reproduce_evaluation(
        self,
        evaluation: StandardsEvaluation,
    ) -> StandardsEvaluation:
        """Re-resolve every authority and replay the canonical evaluator.

        The persisted payload is never trusted as authority: the profile, the
        exact SceneRevision/SystemVariant/entity binding, every criterion
        observation's evidence refs, the reevaluation link, and the canonical
        ``evaluate_standards_profile`` result must all reproduce the record
        exactly or the save/read fails closed.
        """

        profile = self.get_profile(
            evaluation.profile_id,
            evaluation.profile_version,
        )
        if profile is None:
            raise ValueError('standards evaluation references an unsaved StandardsProfile')
        if profile.profile_semantic_hash != evaluation.profile_semantic_hash:
            raise ValueError('standards evaluation profile semantic hash mismatch')

        revision, document, variant = self._resolve_scene_binding(
            subject='standards evaluation',
            document_id=evaluation.target.document_id,
            scene_revision_id=evaluation.target.scene_revision_id,
            scene_content_hash=evaluation.target.scene_content_hash,
            system_variant_id=evaluation.target.system_variant_id,
            system_variant_sha256=evaluation.target.system_variant_sha256,
        )

        entity_ids = {entity.entity_id for entity in document.entities}
        missing_entities = set(evaluation.target.entity_ids) - entity_ids
        if missing_entities:
            raise ValueError(
                'standards evaluation references missing target entities: '
                f'{sorted(missing_entities)}'
            )

        self._resolve_evaluation_evidence(
            evaluation,
            profile,
            scene_revision=revision,
            document=document,
            system_variant=variant,
        )

        if evaluation.reevaluation_of_id is not None:
            previous = self.get_evaluation(evaluation.reevaluation_of_id)
            if previous is None:
                raise ValueError('standards reevaluation references missing historical evaluation')
            if previous.target != evaluation.target:
                raise ValueError('standards reevaluation target must match historical evaluation')

        regenerated = evaluate_standards_profile(
            profile=profile,
            target=evaluation.target,
            observations=evaluation.observations,
            created_at_utc=evaluation.created_at_utc,
            reevaluation_of_id=evaluation.reevaluation_of_id,
        )
        if regenerated != evaluation:
            raise ValueError('standards evaluation does not match evaluator authority')
        return evaluation

    def save_evaluation(
        self,
        evaluation: StandardsEvaluation,
    ) -> StandardsEvaluation:
        evaluation = StandardsEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        # The authority replay resolves profiles, scene rows, and criterion
        # evidence through nested repository connections, so it must finish
        # BEFORE the write transaction begins: opening them while
        # BEGIN IMMEDIATE is held can deadlock the write.
        replayed = self._reproduce_evaluation(evaluation)

        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate
            # recheck and the insert.
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT payload_json FROM cad_standards_evaluations '
                'WHERE evaluation_id=?',
                (replayed.evaluation_id,),
            ).fetchone()
            if row is not None:
                persisted = StandardsEvaluation.model_validate_json(
                    row['payload_json']
                )
                if persisted.evaluation_sha256 != replayed.evaluation_sha256:
                    raise ValueError('standards evaluation identity collision')
                return persisted
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
                    replayed.evaluation_id,
                    replayed.target.document_id,
                    replayed.target.scene_revision_id,
                    replayed.target.system_variant_id,
                    replayed.profile_id,
                    replayed.profile_version,
                    replayed.profile_semantic_hash,
                    replayed.evaluation_sha256,
                    replayed.reevaluation_of_id,
                    replayed.model_dump_json(),
                    replayed.created_at_utc,
                ),
            )
        return replayed

    def _replay_persisted_evaluation(
        self,
        payload_json: str,
    ) -> StandardsEvaluation:
        """Deserialize one persisted row and replay its exact authority."""

        evaluation = StandardsEvaluation.model_validate_json(payload_json)
        return self._reproduce_evaluation(evaluation)

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
        return self._replay_persisted_evaluation(row['payload_json'])

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
            self._replay_persisted_evaluation(row['payload_json'])
            for row in rows
        )
