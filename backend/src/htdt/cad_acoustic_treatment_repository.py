from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

from .cad_acoustic_treatment import (
    AcousticTreatmentDefinition,
    AcousticTreatmentPlacement,
    TreatmentEvidenceAuthority,
    TreatmentEvidenceSubject,
    TreatmentProvenance,
    TreatmentSurfaceBindingEvaluation,
    definition_evidence_subject,
    evaluate_treatment_surface_binding,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    check_native_schema_compatibility,
    require_native_tables,
    connect_sqlite,

)
from .cad_system_variant_repository import CadSystemVariantRepository
from .r120_geometry_compiler import ExactExternalAuthorityRef


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class CadAcousticTreatmentRepository:
    """Append-only persistence for immutable treatment definitions and placements.

    Every provenance claim must resolve to a retained TreatmentEvidenceAuthority
    whose exact source fields and normalized subject support the persisted
    definition. Dangling, mismatched or tampered evidence fails closed on both
    save and read.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.path = Path(scene_repository.path)
        self.assets_dir = self.path.parent / 'measurement-assets'
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        check_native_schema_compatibility(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_acoustic_treatment_definitions', 'cad_acoustic_treatment_placements', 'cad_measurement_assets', 'cad_treatment_evidence_authorities')

    def save_source_asset(self, *, filename: str, data: bytes) -> str:
        """Retain exact treatment source bytes as a managed content-addressed asset.

        The asset lands in the shared measurement-assets store so the native
        backup/restore contract preserves it for replay.
        """

        if not filename:
            raise ValueError('treatment source asset filename is required')
        digest = sha256(data).hexdigest()
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        target = self.assets_dir / digest
        created_asset_file = False
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError('content-addressed treatment source asset hash collision')
        else:
            target.write_bytes(data)
            created_asset_file = True
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    '''INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)''',
                    (
                        digest,
                        filename,
                        str(target.relative_to(self.path.parent)),
                        len(data),
                    ),
                )
        except Exception:
            if created_asset_file:
                target.unlink(missing_ok=True)
            raise
        return digest

    def _check_managed_source_asset(self, digest: str) -> None:
        """Fail closed when a retained managed source asset is missing/tampered.

        A provenance claim whose raw source bytes were never retained resolves
        through the typed evidence authority alone; once an asset row exists the
        managed file must match the claimed hash exactly.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT relative_path, size_bytes FROM cad_measurement_assets '
                'WHERE sha256=?',
                (digest,),
            ).fetchone()
        if row is None:
            return
        root = self.path.parent.resolve()
        target = (self.path.parent / row['relative_path']).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                'managed treatment source asset escapes the data root'
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError('managed treatment source asset is missing')
        if target.stat().st_size != row['size_bytes']:
            raise ValueError('managed treatment source asset size mismatch')
        if _file_sha256(target) != digest:
            raise ValueError('managed treatment source asset SHA-256 mismatch')

    def save_evidence(
        self,
        evidence: TreatmentEvidenceAuthority,
    ) -> TreatmentEvidenceAuthority:
        """Persist an immutable treatment evidence authority."""

        evidence = TreatmentEvidenceAuthority.model_validate(
            evidence.model_dump(mode='python')
        )
        if evidence.source_sha256 is not None:
            self._check_managed_source_asset(evidence.source_sha256)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_treatment_evidence_authorities '
                'WHERE evidence_id=?',
                (evidence.evidence_id,),
            ).fetchone()
            if existing is not None:
                persisted = TreatmentEvidenceAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evidence:
                    raise ValueError(
                        'treatment evidence authority id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_treatment_evidence_authorities(
                    evidence_id, evidence_sha256,
                    source_kind, source_id, source_version, source_sha256,
                    subject_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.source_kind,
                    evidence.source_id,
                    evidence.source_version,
                    evidence.source_sha256,
                    evidence.subject_sha256(),
                    evidence.model_dump_json(),
                ),
            )
        return evidence

    def get_evidence(
        self,
        evidence_id: str,
    ) -> TreatmentEvidenceAuthority | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT evidence_sha256, payload_json '
                'FROM cad_treatment_evidence_authorities WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = TreatmentEvidenceAuthority.model_validate_json(row['payload_json'])
        if (
            evidence.evidence_id != evidence_id
            or evidence.evidence_sha256 != row['evidence_sha256']
        ):
            raise ValueError('persisted treatment evidence authority identity mismatch')
        if evidence.source_sha256 is not None:
            self._check_managed_source_asset(evidence.source_sha256)
        return evidence

    def resolve_evidence(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> TreatmentEvidenceAuthority | None:
        """Resolve an exact authority ref; tampered payloads fail closed."""

        if not ref.authority_id.startswith('treatment-evidence:'):
            return None
        evidence = self.get_evidence(ref.authority_id)
        if evidence is None or evidence.as_external_ref() != ref:
            return None
        return evidence

    def _resolve_provenance_authority(
        self,
        provenance: TreatmentProvenance,
    ) -> TreatmentEvidenceAuthority:
        evidence = self.resolve_evidence(provenance.source_authority)
        if evidence is None:
            raise ValueError(
                'treatment provenance source authority does not resolve to '
                'retained evidence'
            )
        if (
            evidence.source_kind != provenance.source_kind
            or evidence.source_id != provenance.source_id
            or evidence.source_version != provenance.source_version
            or evidence.source_sha256 != provenance.source_sha256
            or evidence.reference != provenance.reference
        ):
            raise ValueError(
                'treatment provenance claim does not match the retained '
                'evidence authority'
            )
        return evidence

    @staticmethod
    def _subject_supports_definition(
        subject: TreatmentEvidenceSubject,
        definition: AcousticTreatmentDefinition,
        *,
        require_acoustic_model: bool,
    ) -> bool:
        if (
            subject.definition_id != definition.definition_id
            or subject.definition_version != definition.version
            or subject.treatment_type != definition.treatment_type
            or subject.dimensions != definition.dimensions
            or float(subject.air_gap_m) != float(definition.air_gap_m)
            or tuple(subject.layers) != tuple(definition.layers)
            or subject.parameters != definition.parameters
        ):
            return False
        expected_model = definition_evidence_subject(definition).acoustic_model
        if subject.acoustic_model is None:
            return not require_acoustic_model
        return (
            expected_model is not None
            and subject.acoustic_model == expected_model
        )

    def resolve_definition_evidence(
        self,
        definition: AcousticTreatmentDefinition,
    ) -> tuple[TreatmentEvidenceAuthority, TreatmentEvidenceAuthority | None]:
        """Resolve every provenance claim of a definition to retained evidence.

        Returns the resolved (definition-level, acoustic-model-level) evidence
        authorities. Missing, mismatched or contradictory evidence raises —
        a definition is never production-authoritative on unresolved claims.
        """

        definition_evidence = self._resolve_provenance_authority(definition.provenance)
        if not self._subject_supports_definition(
            definition_evidence.subject,
            definition,
            require_acoustic_model=False,
        ):
            raise ValueError(
                'treatment definition evidence does not support the normalized '
                'physical authority'
            )
        model_evidence: TreatmentEvidenceAuthority | None = None
        if definition.acoustic_model is not None:
            model_evidence = self._resolve_provenance_authority(
                definition.acoustic_model.provenance
            )
            if not self._subject_supports_definition(
                model_evidence.subject,
                definition,
                require_acoustic_model=True,
            ):
                raise ValueError(
                    'treatment acoustic-model evidence does not support the '
                    'normalized model authority'
                )
        return definition_evidence, model_evidence

    def _validate_definition_authority(
        self,
        definition: AcousticTreatmentDefinition,
    ) -> None:
        self.resolve_definition_evidence(definition)

    def save_definition(
        self,
        definition: AcousticTreatmentDefinition,
    ) -> AcousticTreatmentDefinition:
        definition = AcousticTreatmentDefinition.model_validate(
            definition.model_dump(mode='python')
        )
        self._validate_definition_authority(definition)
        existing = self.get_definition(definition.definition_id, definition.version)
        if existing is not None:
            if existing.definition_sha256 != definition.definition_sha256:
                raise ValueError('AcousticTreatmentDefinition version is immutable')
            return existing

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_acoustic_treatment_definitions(
                    definition_id, definition_version, definition_sha256, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.version,
                    definition.definition_sha256,
                    definition.model_dump_json(),
                ),
            )
        return definition

    def get_definition(
        self,
        definition_id: str,
        version: str,
    ) -> AcousticTreatmentDefinition | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_treatment_definitions '
                'WHERE definition_id=? AND definition_version=?',
                (definition_id, version),
            ).fetchone()
        if row is None:
            return None
        definition = AcousticTreatmentDefinition.model_validate_json(row['payload_json'])
        self._validate_definition_authority(definition)
        return definition

    def list_definition_versions(
        self,
        definition_id: str,
    ) -> tuple[AcousticTreatmentDefinition, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_treatment_definitions '
                'WHERE definition_id=? ORDER BY seq ASC',
                (definition_id,),
            ).fetchall()
        definitions = tuple(
            AcousticTreatmentDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )
        for definition in definitions:
            self._validate_definition_authority(definition)
        return definitions

    def list_definitions(
        self,
    ) -> tuple[AcousticTreatmentDefinition, ...]:
        """Every persisted treatment definition version, reopen-validated (#451)."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_treatment_definitions '
                'ORDER BY seq ASC'
            ).fetchall()
        definitions = tuple(
            AcousticTreatmentDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )
        for definition in definitions:
            self._validate_definition_authority(definition)
        return definitions

    def evaluate_placement_surface_binding(
        self,
        placement: AcousticTreatmentPlacement,
        *,
        scene_revision_id: str | None = None,
    ) -> TreatmentSurfaceBindingEvaluation:
        bound_revision = self.scene_repository.get(placement.scene_revision_id)
        evaluated_revision_id = (
            placement.scene_revision_id
            if scene_revision_id is None
            else scene_revision_id
        )
        evaluated_revision = self.scene_repository.get(evaluated_revision_id)
        return evaluate_treatment_surface_binding(
            placement,
            bound_revision=bound_revision,
            evaluated_revision=evaluated_revision,
            evaluated_revision_id=evaluated_revision_id,
        )

    _PLACEMENT_ROW_COLUMNS = (
        'instance_id, placement_version, lifecycle, '
        'definition_id, definition_version, definition_sha256, '
        'document_id, scene_revision_id, system_variant_id, '
        'placement_sha256, previous_placement_sha256, payload_json'
    )

    def _decode_placement(self, payload_json: str) -> AcousticTreatmentPlacement:
        """Lower-level raw decode: schema validation plus self-hash integrity.

        No external authority is resolved here on purpose: predecessor rows
        are loaded through this decode while replaying lineage, and full
        definition/scene/variant/lineage authority is revalidated separately
        by :meth:`_validate_placement_authority` on every authoritative read.
        """
        return AcousticTreatmentPlacement.model_validate_json(payload_json)

    def _placement_row(
        self,
        instance_id: str,
        placement_version: int,
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                f'SELECT {self._PLACEMENT_ROW_COLUMNS} '
                'FROM cad_acoustic_treatment_placements '
                'WHERE instance_id=? AND placement_version=?',
                (instance_id, placement_version),
            ).fetchone()

    @staticmethod
    def _check_placement_row_identity(
        placement: AcousticTreatmentPlacement,
        row: sqlite3.Row,
    ) -> None:
        """Fail closed when a persisted payload disagrees with its indexed columns."""
        if (
            placement.instance_id != row['instance_id']
            or placement.placement_version != row['placement_version']
            or placement.lifecycle != row['lifecycle']
            or placement.definition_id != row['definition_id']
            or placement.definition_version != row['definition_version']
            or placement.definition_sha256 != row['definition_sha256']
            or placement.document_id != row['document_id']
            or placement.scene_revision_id != row['scene_revision_id']
            or placement.system_variant_id != row['system_variant_id']
            or placement.placement_sha256 != row['placement_sha256']
            or placement.previous_placement_sha256
            != row['previous_placement_sha256']
        ):
            raise ValueError('persisted treatment placement identity mismatch')

    def _decode_placement_row(self, row: sqlite3.Row) -> AcousticTreatmentPlacement:
        placement = self._decode_placement(row['payload_json'])
        self._check_placement_row_identity(placement, row)
        return placement

    def _read_placement_row(self, row: sqlite3.Row) -> AcousticTreatmentPlacement:
        """Authoritative read of one persisted row: decode, identity check,
        then full definition/scene/variant/lineage authority replay."""
        placement = self._decode_placement_row(row)
        self._validate_placement_authority(placement)
        return placement

    def _validate_placement_local_authority(
        self,
        placement: AcousticTreatmentPlacement,
    ) -> None:
        """Re-resolve every external authority one placement version claims.

        The exact AcousticTreatmentDefinition, the exact SceneRevision
        (document/content hash), the semantic host-surface binding and the
        optional SystemVariant (id/hash/baseline relation) are all reloaded
        and compared; missing or mismatched authority fails closed.
        """
        definition = self.get_definition(
            placement.definition_id,
            placement.definition_version,
        )
        if definition is None:
            raise ValueError('treatment placement references an unsaved definition')
        if definition.definition_sha256 != placement.definition_sha256:
            raise ValueError('treatment placement definition semantic hash mismatch')

        revision = self.scene_repository.get(placement.scene_revision_id)
        if revision is None:
            raise ValueError('treatment placement SceneRevision does not exist')
        if (
            revision.document_id != placement.document_id
            or revision.content_hash != placement.scene_content_hash
        ):
            raise ValueError('treatment placement SceneRevision authority mismatch')

        if (
            placement.host_surface_id is not None
            and revision.document.r120_semantic_geometry is not None
        ):
            evaluation = self.evaluate_placement_surface_binding(placement)
            if not evaluation.placement_authority_valid:
                raise ValueError(
                    'treatment placement semantic host binding is invalid: '
                    f'{evaluation.binding_state}'
                )

        if placement.system_variant_id is not None:
            if self.system_variant_repository is None:
                raise ValueError(
                    'SystemVariant-bound treatment placement requires CadSystemVariantRepository'
                )
            variant = self.system_variant_repository.get_variant(placement.system_variant_id)
            if variant is None:
                raise ValueError('treatment placement SystemVariant does not exist')
            if (
                variant.variant_sha256 != placement.system_variant_sha256
                or variant.document_id != placement.document_id
            ):
                raise ValueError('treatment placement SystemVariant authority mismatch')
            if placement.system_variant_relation == 'proposal_baseline' and (
                variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError('proposed treatment placement baseline is not exact')

    def _validate_placement_authority(
        self,
        placement: AcousticTreatmentPlacement,
    ) -> None:
        """Replay full placement authority, including lineage, fail closed.

        Each version's local authority is revalidated, then the exact
        previous-version chain is walked iteratively: predecessor rows are
        loaded through the lower-level raw decode (never through the
        authoritative read path, avoiding recursive read ambiguity) and every
        ancestor is revalidated in turn. Version/cycle guards bound the walk
        and an ``installed`` terminal predecessor rejects any later version.
        """
        visited: set[tuple[str, int]] = set()
        current = placement
        while True:
            key = (current.instance_id, current.placement_version)
            if key in visited:
                raise ValueError('treatment placement lineage cycle detected')
            visited.add(key)

            self._validate_placement_local_authority(current)

            if current.placement_version == 1:
                if (
                    current.previous_placement_version is not None
                    or current.previous_placement_sha256 is not None
                ):
                    raise ValueError(
                        'first treatment placement version cannot have lineage'
                    )
                return

            if (
                current.previous_placement_version != current.placement_version - 1
                or current.previous_placement_sha256 is None
            ):
                raise ValueError(
                    'treatment placement lineage must reference the '
                    'immediately prior version'
                )

            row = self._placement_row(
                current.instance_id,
                current.previous_placement_version,
            )
            if row is None:
                raise ValueError('treatment placement prior lineage does not exist')
            previous = self._decode_placement_row(row)
            if previous.placement_sha256 != current.previous_placement_sha256:
                raise ValueError('treatment placement prior lineage hash mismatch')
            if (
                previous.instance_id != current.instance_id
                or previous.definition_id != current.definition_id
                or previous.definition_version != current.definition_version
                or previous.definition_sha256 != current.definition_sha256
                or previous.document_id != current.document_id
            ):
                raise ValueError(
                    'treatment placement lineage changed immutable authority'
                )
            if previous.lifecycle == 'installed':
                raise ValueError('installed treatment placement is terminal')
            if current.lifecycle not in {'proposed', 'installed'}:
                raise ValueError('unsupported treatment lifecycle transition')
            current = previous

    def save_placement(
        self,
        placement: AcousticTreatmentPlacement,
    ) -> AcousticTreatmentPlacement:
        placement = AcousticTreatmentPlacement.model_validate(
            placement.model_dump(mode='python')
        )
        self._validate_placement_authority(placement)

        existing = self.get_placement(
            placement.instance_id,
            placement.placement_version,
        )
        if existing is not None:
            if existing.placement_sha256 != placement.placement_sha256:
                raise ValueError('treatment placement version is immutable')
            return existing

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                """
                INSERT INTO cad_acoustic_treatment_placements(
                    instance_id, placement_version, lifecycle,
                    definition_id, definition_version, definition_sha256,
                    document_id, scene_revision_id, system_variant_id,
                    placement_sha256, previous_placement_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    placement.instance_id,
                    placement.placement_version,
                    placement.lifecycle,
                    placement.definition_id,
                    placement.definition_version,
                    placement.definition_sha256,
                    placement.document_id,
                    placement.scene_revision_id,
                    placement.system_variant_id,
                    placement.placement_sha256,
                    placement.previous_placement_sha256,
                    placement.model_dump_json(),
                ),
            )
        return placement

    def get_placement(
        self,
        instance_id: str,
        placement_version: int,
    ) -> AcousticTreatmentPlacement | None:
        row = self._placement_row(instance_id, placement_version)
        if row is None:
            return None
        return self._read_placement_row(row)

    def latest_placement(
        self,
        instance_id: str,
    ) -> AcousticTreatmentPlacement | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f'SELECT {self._PLACEMENT_ROW_COLUMNS} '
                'FROM cad_acoustic_treatment_placements '
                'WHERE instance_id=? ORDER BY placement_version DESC LIMIT 1',
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._read_placement_row(row)

    def list_placements_for_scene(
        self,
        scene_revision_id: str,
    ) -> tuple[AcousticTreatmentPlacement, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f'SELECT {self._PLACEMENT_ROW_COLUMNS} '
                'FROM cad_acoustic_treatment_placements '
                'WHERE scene_revision_id=? ORDER BY seq ASC',
                (scene_revision_id,),
            ).fetchall()
        return tuple(self._read_placement_row(row) for row in rows)

    def proposed_placement_ids(self, document_id: str) -> tuple[str, ...]:
        """Instance ids whose *latest* placement version is still proposed.

        An installed lifecycle supersedes the proposal — those placements
        are physical/as-built facts and never come back through this view.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT instance_id FROM cad_acoustic_treatment_placements p
                WHERE document_id=?
                  AND p.placement_version = (
                      SELECT MAX(placement_version)
                      FROM cad_acoustic_treatment_placements
                      WHERE instance_id=p.instance_id
                  )
                  AND p.lifecycle='proposed'
                ORDER BY p.seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(str(row['instance_id']) for row in rows)

    def delete_placement(self, instance_id: str) -> None:
        """Remove a still-proposed placement and every version beneath it.

        Install facts are never deletable through this path: an instance
        whose latest version is ``installed`` (or anything other than
        ``proposed``) raises instead of silently erasing a lifecycle
        record. This exists for the Room design-transaction Discard — a
        proposed placement created inside a dirty session is rolled back
        with the rest of the uncommitted design state.
        """
        latest = self.latest_placement(instance_id)
        if latest is None:
            return
        if latest.lifecycle != 'proposed':
            raise ValueError(
                f'placement {instance_id} is {latest.lifecycle}, not proposed — '
                'physical lifecycle facts are not deletable'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_acoustic_treatment_placements '
                'WHERE instance_id=?',
                (instance_id,),
            )

    def list_placements_for_variant(
        self,
        system_variant_id: str,
    ) -> tuple[AcousticTreatmentPlacement, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f'SELECT {self._PLACEMENT_ROW_COLUMNS} '
                'FROM cad_acoustic_treatment_placements '
                'WHERE system_variant_id=? ORDER BY seq ASC',
                (system_variant_id,),
            ).fetchall()
        return tuple(self._read_placement_row(row) for row in rows)
