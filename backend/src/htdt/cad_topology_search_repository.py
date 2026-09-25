from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_topology_space import TopologySearchSpec
from .cad_topology_search import (
    TopologyPlacementCandidate,
    TopologyPlacementCandidateSetPage,
    TopologyPlacementSearchSpec,
    build_topology_placement_search_spec,
    declared_base_constraint_set,
    generate_topology_placement_candidates,

)

from .cad_schema import require_native_tables


class TopologyPlacementComparisonRef(BaseModel):
    """Identity-only bridge into existing comparison/objective authorities."""

    model_config = ConfigDict(frozen=True)

    search_id: str = Field(min_length=1)
    search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    template_variant_id: str = Field(min_length=1)
    template_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_search_id: str = Field(min_length=1)
    topology_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_option_id: str = Field(min_length=1)
    variant_id: str | None = Field(default=None, min_length=1)
    variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    proposed_content_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    applied_revision_id: str | None = Field(default=None, min_length=1)
    applied_content_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def valid_variant_ref(self) -> 'TopologyPlacementComparisonRef':
        variant_fields = (
            self.variant_id,
            self.variant_sha256,
            self.proposed_content_hash,
        )
        if any(value is None for value in variant_fields) and any(
            value is not None for value in variant_fields
        ):
            raise ValueError('candidate variant comparison fields must be supplied together')
        if (self.applied_revision_id is None) != (self.applied_content_hash is None):
            raise ValueError('applied revision/hash must be supplied together')
        if self.variant_id is None and self.applied_revision_id is not None:
            raise ValueError('applied revision requires a persisted candidate variant')
        return self


class CadTopologySearchRepository:
    """O100B persistence layered on O100A variant and SceneRevision authority."""

    def __init__(self, variant_repository: CadSystemVariantRepository) -> None:
        self.variant_repository = variant_repository
        self.scene_repository = variant_repository.scene_repository
        self.path = Path(self.scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_topology_spaces', 'cad_topology_space_options', 'cad_topology_search_specs', 'cad_topology_placement_candidates', 'cad_topology_candidate_variants')

    def save_topology_spec(self, spec: TopologySearchSpec) -> None:
        spec = TopologySearchSpec.model_validate(spec.model_dump(mode='python'))
        baseline = self.scene_repository.get(spec.baseline_revision_id)
        if baseline is None:
            raise ValueError('TopologySearchSpec baseline SceneRevision does not exist')
        if (
            baseline.document_id != spec.document_id
            or baseline.content_hash != spec.baseline_content_hash
        ):
            raise ValueError('TopologySearchSpec baseline authority mismatch')

        for option in spec.options:
            variant = self.variant_repository.get_variant(
                option.template_variant_id
            )
            if variant is None:
                raise ValueError(
                    'TopologySearchSpec option SystemVariant is not persisted'
                )
            if (
                variant.variant_sha256 != option.template_variant_sha256
                or variant.baseline_revision_id != spec.baseline_revision_id
                or variant.baseline_content_hash != spec.baseline_content_hash
            ):
                raise ValueError(
                    'TopologySearchSpec option SystemVariant authority mismatch'
                )

        existing = self.get_topology_spec(spec.topology_search_id)
        if existing is not None:
            if existing != spec:
                raise ValueError(
                    'topology_search_id already stores different payload'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_topology_spaces(
                    topology_search_id, topology_search_sha256,
                    document_id, baseline_revision_id,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.topology_search_id,
                    spec.topology_search_sha256,
                    spec.document_id,
                    spec.baseline_revision_id,
                    spec.model_dump_json(),
                    spec.created_at_utc,
                ),
            )
            for option in spec.options:
                connection.execute(
                    """
                    INSERT INTO cad_topology_space_options(
                        topology_search_id, option_id, template_variant_id
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        spec.topology_search_id,
                        option.option_id,
                        option.template_variant_id,
                    ),
                )

    def get_topology_spec(
        self,
        topology_search_id: str,
    ) -> TopologySearchSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_topology_spaces '
                'WHERE topology_search_id=?',
                (topology_search_id,),
            ).fetchone()
        return (
            None
            if row is None
            else TopologySearchSpec.model_validate_json(row['payload_json'])
        )

    def _require_placement_spec_authority(
        self,
        spec: TopologyPlacementSearchSpec,
    ) -> None:
        """Recompile the embedded O100B execution payloads from exact inputs.

        Resolves the baseline SceneRevision, the persisted TopologySearchSpec
        and option, and the template SystemVariant; recovers the declared
        base CadConstraintSet from the embedded snapshot; then reruns the
        pinned build_topology_placement_search_spec compiler over the
        declared placement inputs and requires the submitted spec to be that
        exact canonical compilation. Tampered or non-canonical payloads fail
        closed; used by both save-time validation and authoritative reads.
        """

        baseline = self.scene_repository.get(spec.baseline_revision_id)
        if baseline is None:
            raise ValueError('topology search baseline SceneRevision does not exist')
        if (
            baseline.document_id != spec.document_id
            or baseline.content_hash != spec.baseline_content_hash
        ):
            raise ValueError('topology search baseline SceneRevision authority mismatch')
        topology = self.get_topology_spec(spec.topology_search_id)
        if topology is None:
            raise ValueError(
                'TopologySearchSpec must be persisted before placement search'
            )
        if topology.topology_search_sha256 != spec.topology_search_sha256:
            raise ValueError('topology placement TopologySearchSpec hash mismatch')
        try:
            option = topology.option(spec.topology_option_id)
        except KeyError as exc:
            raise ValueError(
                'topology placement references unknown topology option'
            ) from exc
        if (
            option.template_variant_id != spec.template_variant_id
            or option.template_variant_sha256 != spec.template_variant_sha256
        ):
            raise ValueError('topology placement option/template authority mismatch')

        template = self.variant_repository.get_variant(spec.template_variant_id)
        if template is None:
            raise ValueError('topology search template SystemVariant is not persisted')
        if template.variant_sha256 != spec.template_variant_sha256:
            raise ValueError('topology search template SystemVariant hash mismatch')

        expected = build_topology_placement_search_spec(
            baseline=baseline,
            template_variant=template,
            topology_spec=topology,
            topology_option_id=spec.topology_option_id,
            placement_specs=spec.placement_specs,
            constraint_set=declared_base_constraint_set(spec),
            linked_rules=spec.linked_rules,
            candidate_limit=spec.candidate_limit,
            created_at_utc=spec.created_at_utc,
        )
        if expected != spec:
            raise ValueError(
                'topology placement search spec is not the canonical '
                'compilation of its declared placement authority'
            )

    def save_spec(self, spec: TopologyPlacementSearchSpec) -> None:
        spec = TopologyPlacementSearchSpec.model_validate(
            spec.model_dump(mode='python')
        )
        self._require_placement_spec_authority(spec)

        existing = self.get_spec(spec.search_id)
        if existing is not None:
            if existing != spec:
                raise ValueError('topology search_id already stores different payload')
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_topology_search_specs(
                    search_id, search_sha256, document_id,
                    baseline_revision_id, template_variant_id,
                    topology_search_id, topology_option_id,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.search_id,
                    spec.search_sha256,
                    spec.document_id,
                    spec.baseline_revision_id,
                    spec.template_variant_id,
                    spec.topology_search_id,
                    spec.topology_option_id,
                    spec.model_dump_json(),
                    spec.created_at_utc,
                ),
            )

    def _validated_spec(self, row: sqlite3.Row) -> TopologyPlacementSearchSpec:
        """Deserialize one persisted spec row and replay its exact authority."""

        spec = TopologyPlacementSearchSpec.model_validate_json(
            row['payload_json']
        )
        if (
            row['search_id'] != spec.search_id
            or row['search_sha256'] != spec.search_sha256
            or row['document_id'] != spec.document_id
            or row['baseline_revision_id'] != spec.baseline_revision_id
            or row['template_variant_id'] != spec.template_variant_id
            or row['topology_search_id'] != spec.topology_search_id
            or row['topology_option_id'] != spec.topology_option_id
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted topology placement search row disagrees with its payload'
            )
        self._require_placement_spec_authority(spec)
        return spec

    def get_spec(self, search_id: str) -> TopologyPlacementSearchSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_topology_search_specs WHERE search_id=?',
                (search_id,),
            ).fetchone()
        return None if row is None else self._validated_spec(row)

    def list_specs(self, document_id: str) -> tuple[TopologyPlacementSearchSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_topology_search_specs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._validated_spec(row) for row in rows)

    def _rederived_candidate_page(
        self,
        spec: TopologyPlacementSearchSpec,
        *,
        offset: int,
        limit: int,
    ) -> TopologyPlacementCandidateSetPage:
        """Regenerate one canonical candidate page from persisted authority.

        Resolves the baseline SceneRevision and template SystemVariant named
        by the already-replayed search spec and reruns the deterministic
        O10/G10/O80 generator over the exact persisted inputs. The result is
        the only authoritative page for the requested window: candidate
        payloads/SHAs/IDs, feasible indices, the full candidate-set digest,
        and all count/rejection metadata are recomputed rather than trusted
        from a caller or a stored row.
        """

        baseline = self.scene_repository.get(spec.baseline_revision_id)
        if baseline is None:
            raise ValueError('topology search baseline SceneRevision does not exist')
        template = self.variant_repository.get_variant(spec.template_variant_id)
        if template is None:
            raise ValueError('topology search template SystemVariant is not persisted')
        return generate_topology_placement_candidates(
            baseline=baseline,
            template_variant=template,
            spec=spec,
            offset=offset,
            limit=limit,
        )

    def save_candidate_page(
        self,
        page: TopologyPlacementCandidateSetPage,
    ) -> None:
        page = TopologyPlacementCandidateSetPage.model_validate(
            page.model_dump(mode='python')
        )
        spec = self.get_spec(page.search_id)
        if spec is None:
            raise ValueError('topology search spec must be persisted before candidates')
        if page.search_sha256 != spec.search_sha256:
            raise ValueError('topology candidate page search hash mismatch')

        # Candidate persistence rederives exact generator authority: the
        # submitted page must equal the canonical regeneration of the
        # persisted search spec, so fabricated candidates, forged
        # candidate-set digests, and mismatched count/rejection metadata
        # fail closed. One regeneration validates the whole batch, which is
        # then persisted atomically.
        expected = self._rederived_candidate_page(
            spec,
            offset=page.offset,
            limit=page.limit,
        )
        if page != expected:
            raise ValueError(
                'topology candidate page is not the canonical regeneration '
                'of the persisted placement search authority'
            )

        with closing(self._connect()) as connection, connection:
            for candidate in expected.candidates:
                row = connection.execute(
                    'SELECT candidate_set_sha256, payload_json '
                    'FROM cad_topology_placement_candidates '
                    'WHERE candidate_id=?',
                    (candidate.candidate_id,),
                ).fetchone()
                if row is not None:
                    stored = TopologyPlacementCandidate.model_validate_json(
                        row['payload_json']
                    )
                    if (
                        stored != candidate
                        or row['candidate_set_sha256']
                        != expected.candidate_set_sha256
                    ):
                        raise ValueError(
                            'deterministic topology candidate identity collision'
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO cad_topology_placement_candidates(
                        candidate_id, candidate_sha256, search_id,
                        candidate_set_sha256, feasible_index, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.candidate_id,
                        candidate.candidate_sha256,
                        candidate.search_id,
                        expected.candidate_set_sha256,
                        candidate.feasible_index,
                        candidate.model_dump_json(),
                    ),
                )

    def _validated_candidate(self, row: sqlite3.Row) -> TopologyPlacementCandidate:
        """Deserialize one persisted candidate row and replay its membership.

        The row columns must agree with the payload, the persisted spec must
        still replay its own canonical authority, and the payload must be
        the exact deterministic member of the regenerated candidate set at
        its feasible index with the stored candidate-set digest. Fabricated
        or tampered rows fail closed instead of being returned as
        authoritative.
        """

        candidate = TopologyPlacementCandidate.model_validate_json(
            row['payload_json']
        )
        if (
            row['candidate_id'] != candidate.candidate_id
            or row['candidate_sha256'] != candidate.candidate_sha256
            or row['search_id'] != candidate.search_id
            or row['feasible_index'] != candidate.feasible_index
        ):
            raise ValueError(
                'persisted topology candidate row disagrees with its payload'
            )
        spec = self.get_spec(candidate.search_id)
        if spec is None:
            raise ValueError('topology candidate search spec is missing')
        page = self._rederived_candidate_page(
            spec,
            offset=candidate.feasible_index,
            limit=1,
        )
        if page.candidate_set_sha256 != row['candidate_set_sha256']:
            raise ValueError('topology candidate-set authority mismatch')
        if page.candidates != (candidate,):
            raise ValueError(
                'topology candidate is not an exact deterministic search member'
            )
        return candidate

    def get_candidate(
        self,
        candidate_id: str,
    ) -> TopologyPlacementCandidate | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_topology_placement_candidates '
                'WHERE candidate_id=?',
                (candidate_id,),
            ).fetchone()
        return None if row is None else self._validated_candidate(row)

    def list_candidates(
        self,
        search_id: str,
    ) -> tuple[TopologyPlacementCandidate, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_topology_placement_candidates '
                'WHERE search_id=? ORDER BY feasible_index ASC',
                (search_id,),
            ).fetchall()
        return tuple(self._validated_candidate(row) for row in rows)

    def save_candidate_variant(
        self,
        candidate_id: str,
        variant: SystemVariant,
    ) -> None:
        """Publish one candidate-derived SystemVariant and its mapping atomically.

        Candidate/spec authority and the SystemVariant's own authority are
        replayed on auxiliary connections BEFORE the write transaction opens:
        those lookups run on nested connections, which must not execute while
        BEGIN IMMEDIATE is held. The current-mapping check, the conditional
        variant insert, and the candidate->variant mapping insert then commit
        or roll back under one write transaction, so a failure can never
        leave a persisted variant without its candidate binding.
        """

        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError('topology candidate must be persisted before its variant')
        spec = self.get_spec(candidate.search_id)
        if spec is None:
            raise ValueError('topology candidate search spec is missing')
        variant = SystemVariant.model_validate(variant.model_dump(mode='python'))
        if (
            variant.document_id != spec.document_id
            or variant.baseline_revision_id != spec.baseline_revision_id
            or variant.baseline_content_hash != spec.baseline_content_hash
        ):
            raise ValueError('candidate SystemVariant baseline authority mismatch')
        if variant.parent_variant_id != spec.template_variant_id:
            raise ValueError('candidate SystemVariant must descend from topology template')
        provenance = {item.key: item.value for item in variant.provenance}
        if (
            provenance.get('o100b.topology_search_sha256')
            != spec.topology_search_sha256
            or provenance.get('o100b.topology_option_id')
            != spec.topology_option_id
            or provenance.get('o100b.search_sha256') != spec.search_sha256
            or provenance.get('o100b.candidate_id') != candidate.candidate_id
            or provenance.get('o100b.candidate_sha256') != candidate.candidate_sha256
        ):
            raise ValueError('candidate SystemVariant O100B provenance mismatch')

        # A variant that is not yet persisted must prove its own authority
        # before the write transaction opens. Inside the transaction the row
        # is only re-checked, so a racing writer that committed an identical
        # variant is reused while a conflicting payload fails closed.
        existing_variant = self.variant_repository.get_variant(variant.variant_id)
        if existing_variant is None:
            self.variant_repository._require_variant_authority(variant)
        elif existing_variant != variant:
            raise ValueError('candidate variant_id already stores different payload')

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT variant_id FROM cad_topology_candidate_variants '
                'WHERE candidate_id=?',
                (candidate_id,),
            ).fetchone()
            if row is not None:
                if row['variant_id'] != variant.variant_id:
                    raise ValueError(
                        'topology candidate already maps to another SystemVariant'
                    )
                return
            variant_row = connection.execute(
                'SELECT payload_json FROM cad_system_variants '
                'WHERE variant_id=?',
                (variant.variant_id,),
            ).fetchone()
            if variant_row is None:
                self.variant_repository._save_variant_in_transaction(
                    connection,
                    variant,
                )
            elif (
                SystemVariant.model_validate_json(variant_row['payload_json'])
                != variant
            ):
                raise ValueError(
                    'candidate variant_id already stores different payload'
                )
            connection.execute(
                'INSERT INTO cad_topology_candidate_variants('
                'candidate_id, variant_id) VALUES (?, ?)',
                (candidate_id, variant.variant_id),
            )

    def variant_for_candidate(self, candidate_id: str) -> SystemVariant | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT variant_id FROM cad_topology_candidate_variants '
                'WHERE candidate_id=?',
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        variant = self.variant_repository.get_variant(row['variant_id'])
        if variant is None:
            raise ValueError('topology candidate mapping references missing variant')
        return variant

    def comparison_ref(
        self,
        candidate_id: str,
    ) -> TopologyPlacementComparisonRef:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError('topology placement candidate does not exist')
        spec = self.get_spec(candidate.search_id)
        if spec is None:
            raise ValueError('topology placement search spec does not exist')
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT candidate_set_sha256 '
                'FROM cad_topology_placement_candidates '
                'WHERE candidate_id=?',
                (candidate_id,),
            ).fetchone()
        assert row is not None

        variant = self.variant_for_candidate(candidate_id)
        if variant is None:
            return TopologyPlacementComparisonRef(
                search_id=spec.search_id,
                search_sha256=spec.search_sha256,
                candidate_id=candidate.candidate_id,
                candidate_sha256=candidate.candidate_sha256,
                candidate_set_sha256=row['candidate_set_sha256'],
                baseline_revision_id=spec.baseline_revision_id,
                baseline_content_hash=spec.baseline_content_hash,
                template_variant_id=spec.template_variant_id,
                template_variant_sha256=spec.template_variant_sha256,
                topology_search_id=spec.topology_search_id,
                topology_search_sha256=spec.topology_search_sha256,
                topology_option_id=spec.topology_option_id,
            )

        comparison = self.variant_repository.comparison_ref(variant.variant_id)
        return TopologyPlacementComparisonRef(
            search_id=spec.search_id,
            search_sha256=spec.search_sha256,
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.candidate_sha256,
            candidate_set_sha256=row['candidate_set_sha256'],
            baseline_revision_id=spec.baseline_revision_id,
            baseline_content_hash=spec.baseline_content_hash,
            template_variant_id=spec.template_variant_id,
            template_variant_sha256=spec.template_variant_sha256,
            topology_search_id=spec.topology_search_id,
            topology_search_sha256=spec.topology_search_sha256,
            topology_option_id=spec.topology_option_id,
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
            proposed_content_hash=comparison.proposed_content_hash,
            applied_revision_id=comparison.applied_revision_id,
            applied_content_hash=comparison.applied_content_hash,
        )
