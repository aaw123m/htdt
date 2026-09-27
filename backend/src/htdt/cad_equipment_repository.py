from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_equipment import (
    EquipmentCapabilityClaim,
    EquipmentCapabilityResult,
    EquipmentDataProvenance,
    EquipmentDefinition,
    evaluate_equipment_capability,
    equipment_capability_matrix,
)
from .cad_equipment_catalog import (
    EquipmentCatalogSnapshot,
    build_equipment_catalog_snapshot,
)
from .cad_equipment_evidence import (
    EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
    EQUIPMENT_STRUCTURAL_FIELD_GROUPS,
    EquipmentEvidenceAuthority,
    EquipmentEvidenceFieldGroup,
    EquipmentEvidenceSubject,
    EquipmentFieldEvidenceRef,
    EquipmentUncertaintySubject,
    ResolvedEquipmentEvidence,
    equipment_field_group_subjects,
    equipment_provenance_items,
    provenance_sha256,
    replay_equipment_evidence_extraction,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .cad_system_variant import (
    EquipmentBindingRef,
    SystemVariant,
    materialize_system_variant,
)
from .cad_system_variant_repository import CadSystemVariantRepository
from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetStore,
    verify_managed_asset,
)
from .clock import utc_now_iso as _utc_now


class ResolvedEquipmentBinding(BaseModel):
    """Exact SystemVariant source-entity -> EquipmentDefinition resolution."""

    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_id: str = Field(min_length=1)
    binding: EquipmentBindingRef
    definition: EquipmentDefinition


class CadEquipmentRepository:
    """Append-only O100C EquipmentDefinition persistence and exact variant resolution.

    A persisted EquipmentDefinition is only authoritative when every
    ``EquipmentDataProvenance`` it cites resolves to a retained
    ``EquipmentEvidenceAuthority`` bound to the exact definition SHA-256 whose
    recorded subject reproduces the definition's normalized field-group
    values. Managed source assets are verified byte-exact (and replayed when
    the registered extractor is named); missing, mismatched or tampered
    evidence fails closed on save and on every authoritative read, so a
    fabricated ``source_sha256`` cannot authorize production
    sensitivity/SPL/uncertainty data.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self.assets_dir = self.path.parent / MANAGED_ASSETS_DIRNAME
        self._asset_store = ManagedAssetStore(self.assets_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_equipment_definitions', 'cad_measurement_assets', 'cad_equipment_evidence_authorities')

    # ------------------------------------------------------------------
    # Managed source assets (shared measurement-assets contract, #357)

    def _verified_asset_file(
        self,
        digest: str,
        *,
        relative_path: str,
        size_bytes: int,
    ) -> bytes:
        """Reopen a registered managed asset, failing closed on tampering."""
        asset_path = verify_managed_asset(
            data_dir=self.path.parent,
            digest=digest,
            relative_path=relative_path,
            size_bytes=size_bytes,
            required_root=self.assets_dir,
        )
        if asset_path.name != digest:
            raise ValueError(
                'managed equipment source asset path does not match its '
                f'content address: {relative_path}'
            )
        return self._asset_store.read_file(asset_path)

    def _verified_source_asset(self, digest: str) -> bytes:
        """Reopen the exact retained source bytes for *digest*.

        A registered asset whose file is missing, resized or tampered fails
        closed; an unregistered digest raises rather than treating a bare
        ``source_sha256`` claim as retained evidence.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT relative_path, size_bytes
                FROM cad_measurement_assets
                WHERE sha256=?
                """,
                (digest,),
            ).fetchone()
        if row is None:
            raise ValueError(
                'equipment source asset is not registered in the managed '
                'asset store'
            )
        return self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def _check_optional_source_asset(self, digest: str) -> None:
        """Verify a managed asset row for *digest* when one exists.

        Manual/external evidence records do not require retained bytes, but
        once a managed asset row exists for the claimed source hash the file
        must match it exactly — a divergent managed asset can never silently
        coexist with a record that names it.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT relative_path, size_bytes FROM cad_measurement_assets '
                'WHERE sha256=?',
                (digest,),
            ).fetchone()
        if row is None:
            return
        self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def read_source_asset(self, source_asset_sha256: str) -> bytes | None:
        """Reopen the exact retained source bytes for a content address.

        Returns ``None`` only when no managed asset is registered for the
        digest; a registered asset whose file is missing, resized or
        tampered raises instead of returning unverifiable bytes.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT relative_path, size_bytes
                FROM cad_measurement_assets
                WHERE sha256=?
                """,
                (source_asset_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    # ------------------------------------------------------------------
    # Evidence authorities

    def _reverify_evidence(self, evidence: EquipmentEvidenceAuthority) -> None:
        """Re-resolve and re-derive one evidence authority, failing closed.

        Managed source bytes are re-verified (and replayed through the
        recorded extractor when it is the registered normalized-JSON
        extractor, so the recorded subject must reproduce exactly from the
        retained bytes). Manual/external records are immutable and
        self-hashed; a managed asset registered under their claimed source
        hash must still verify.
        """
        if evidence.authority_kind == 'managed_source_asset':
            asset = evidence.managed_asset
            assert asset is not None  # enforced by model validation
            bound = self._verified_source_asset(asset.source_asset_sha256)
            if len(bound) != asset.size_bytes:
                raise ValueError(
                    'equipment evidence managed asset size mismatch'
                )
            if asset.extractor_id == EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID:
                replayed = replay_equipment_evidence_extraction(
                    source_bytes=bound,
                    extractor_id=asset.extractor_id,
                    extractor_version=asset.extractor_version,
                )
                if replayed != evidence.subject:
                    raise ValueError(
                        'equipment evidence subject does not replay from its '
                        'retained source asset'
                    )
        else:
            self._check_optional_source_asset(evidence.provenance.source_sha256)

    def save_evidence(
        self,
        evidence: EquipmentEvidenceAuthority,
        *,
        source_bytes: bytes | None = None,
        source_filename: str | None = None,
        media_type: str | None = None,
        declared_schema: str | None = None,
    ) -> EquipmentEvidenceAuthority:
        """Persist an immutable equipment evidence authority.

        For ``managed_source_asset`` evidence the exact source bytes must be
        supplied (installed atomically into the shared managed asset store)
        or already retained under the bound digest; either way they are
        re-verified and — for the registered extractor — replayed against the
        recorded subject before anything is committed. A provenance claim
        already backed by different retained evidence for the same definition
        fails closed.
        """
        evidence = EquipmentEvidenceAuthority.model_validate(
            evidence.model_dump(mode='python')
        )

        bound_source: bytes | None = None
        install_digest: str | None = None
        if evidence.authority_kind == 'managed_source_asset':
            asset = evidence.managed_asset
            assert asset is not None
            digest = asset.source_asset_sha256
            if source_bytes is not None:
                if not isinstance(source_bytes, bytes):
                    raise TypeError(
                        'equipment evidence source payload must be bytes'
                    )
                if sha256(source_bytes).hexdigest() != digest:
                    raise ValueError(
                        'equipment evidence source bytes do not match '
                        'the bound source asset hash'
                    )
                if len(source_bytes) != asset.size_bytes:
                    raise ValueError(
                        'equipment evidence source bytes do not match '
                        'the recorded asset size'
                    )
                if not source_filename:
                    raise ValueError(
                        'equipment evidence source filename is required when '
                        'persisting source bytes'
                    )
                if (
                    asset.filename is not None
                    and asset.filename != source_filename
                ):
                    raise ValueError(
                        'equipment evidence asset filename mismatch'
                    )
                if (
                    asset.media_type is not None
                    and media_type is not None
                    and asset.media_type != media_type
                ):
                    raise ValueError(
                        'equipment evidence asset media type mismatch'
                    )
                if (
                    asset.declared_schema is not None
                    and declared_schema is not None
                    and asset.declared_schema != declared_schema
                ):
                    raise ValueError(
                        'equipment evidence asset declared schema mismatch'
                    )
                # Install before the transaction: a failed commit leaves a
                # safe content-addressed orphan rather than a partial file.
                self._asset_store.ensure_installed(digest, source_bytes)
                bound_source = source_bytes
                install_digest = digest
            else:
                bound_source = self._verified_source_asset(digest)
                if len(bound_source) != asset.size_bytes:
                    raise ValueError(
                        'equipment evidence managed asset size mismatch'
                    )
            if asset.extractor_id == EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID:
                replayed = replay_equipment_evidence_extraction(
                    source_bytes=bound_source,
                    extractor_id=asset.extractor_id,
                    extractor_version=asset.extractor_version,
                )
                if replayed != evidence.subject:
                    raise ValueError(
                        'equipment evidence subject does not replay from its '
                        'retained source asset'
                    )
        else:
            if source_bytes is not None:
                raise ValueError(
                    'manual/external equipment evidence cannot carry raw '
                    'source bytes'
                )
            self._check_optional_source_asset(
                evidence.provenance.source_sha256
            )

        # When the bound definition is already persisted, the recorded
        # subject must not diverge from its normalized values.
        persisted_definition = self._definition_row_by_hash(
            evidence.equipment_definition_sha256
        )
        if persisted_definition is not None:
            if (
                persisted_definition.definition_id
                != evidence.equipment_definition_id
                or persisted_definition.version
                != evidence.equipment_definition_version
            ):
                raise ValueError(
                    'equipment evidence definition identity mismatch'
                )
            self._assert_subject_consistent(
                evidence.subject,
                persisted_definition,
            )
            if not any(
                item == evidence.provenance
                for item in equipment_provenance_items(persisted_definition)
            ):
                raise ValueError(
                    'equipment evidence provenance is not cited by the bound '
                    'EquipmentDefinition'
                )

        prov_sha = provenance_sha256(evidence.provenance)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_evidence_authorities
                WHERE evidence_id=?
                """,
                (evidence.evidence_id,),
            ).fetchone()
            if existing is not None:
                persisted = EquipmentEvidenceAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evidence:
                    raise ValueError(
                        'equipment evidence authority id exists with '
                        'different semantics'
                    )
                return persisted
            conflict = connection.execute(
                """
                SELECT evidence_id
                FROM cad_equipment_evidence_authorities
                WHERE equipment_definition_sha256=? AND provenance_sha256=?
                """,
                (evidence.equipment_definition_sha256, prov_sha),
            ).fetchone()
            if conflict is not None:
                raise ValueError(
                    'different equipment evidence is already retained for '
                    'this definition provenance claim'
                )
            if install_digest is not None and bound_source is not None:
                target = self._asset_store.asset_path(install_digest)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        install_digest,
                        source_filename or '',
                        str(target.relative_to(self.path.parent)),
                        len(bound_source),
                    ),
                )
            connection.execute(
                """
                INSERT INTO cad_equipment_evidence_authorities(
                    evidence_id, evidence_sha256,
                    equipment_definition_sha256, provenance_sha256,
                    source_sha256, authority_kind, subject_sha256,
                    field_groups_json, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.equipment_definition_sha256,
                    prov_sha,
                    evidence.provenance.source_sha256,
                    evidence.authority_kind,
                    evidence.subject_sha256(),
                    json.dumps(
                        list(evidence.field_groups()),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(',', ':'),
                    ),
                    evidence.model_dump_json(),
                    _utc_now(),
                ),
            )
        return evidence

    def _decode_evidence_row(self, row: sqlite3.Row) -> EquipmentEvidenceAuthority:
        evidence = EquipmentEvidenceAuthority.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.equipment_definition_sha256
            != row['equipment_definition_sha256']
            or provenance_sha256(evidence.provenance)
            != row['provenance_sha256']
            or evidence.provenance.source_sha256 != row['source_sha256']
            or evidence.authority_kind != row['authority_kind']
            or evidence.subject_sha256() != row['subject_sha256']
            or json.loads(row['field_groups_json'])
            != list(evidence.field_groups())
        ):
            raise ValueError(
                'persisted equipment evidence authority column mismatch'
            )
        self._reverify_evidence(evidence)
        return evidence

    def get_evidence(
        self,
        evidence_id: str,
    ) -> EquipmentEvidenceAuthority | None:
        """Return a retained evidence authority, re-verified fail-closed."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evidence_id, evidence_sha256,
                       equipment_definition_sha256, provenance_sha256,
                       source_sha256, authority_kind, subject_sha256,
                       field_groups_json, payload_json
                FROM cad_equipment_evidence_authorities
                WHERE evidence_id=?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_evidence_row(row)

    def list_evidence_for_definition(
        self,
        equipment_definition_sha256: str,
    ) -> tuple[EquipmentEvidenceAuthority, ...]:
        """All retained evidence authorities bound to one exact definition."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, evidence_sha256,
                       equipment_definition_sha256, provenance_sha256,
                       source_sha256, authority_kind, subject_sha256,
                       field_groups_json, payload_json
                FROM cad_equipment_evidence_authorities
                WHERE equipment_definition_sha256=?
                ORDER BY seq ASC
                """,
                (equipment_definition_sha256,),
            ).fetchall()
        return tuple(self._decode_evidence_row(row) for row in rows)

    def _resolve_provenance_authority(
        self,
        equipment_definition_sha256: str,
        provenance: EquipmentDataProvenance,
    ) -> EquipmentEvidenceAuthority:
        """Resolve one provenance claim to retained evidence, failing closed."""
        prov_sha = provenance_sha256(provenance)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evidence_id, evidence_sha256,
                       equipment_definition_sha256, provenance_sha256,
                       source_sha256, authority_kind, subject_sha256,
                       field_groups_json, payload_json
                FROM cad_equipment_evidence_authorities
                WHERE equipment_definition_sha256=? AND provenance_sha256=?
                """,
                (equipment_definition_sha256, prov_sha),
            ).fetchone()
        if row is None:
            raise ValueError(
                'equipment provenance claim does not resolve to retained '
                'evidence'
            )
        evidence = self._decode_evidence_row(row)
        if evidence.provenance != provenance:
            raise ValueError(
                'retained equipment evidence provenance does not match the '
                'cited claim'
            )
        return evidence

    @staticmethod
    def _assert_subject_consistent(
        subject: EquipmentEvidenceSubject,
        definition: EquipmentDefinition,
    ) -> None:
        """Every recorded subject value must reproduce the definition exactly."""
        expected = equipment_field_group_subjects(definition)
        for group in subject.supported_groups():
            if group == 'uncertainty':
                allowed = {
                    EquipmentUncertaintySubject.from_item(item)
                    for item in definition.uncertainty
                }
                for entry in subject.uncertainty:
                    if entry not in allowed:
                        raise ValueError(
                            'equipment evidence subject diverges from the '
                            'bound EquipmentDefinition'
                        )
                continue
            if group not in expected or subject.group_value(group) != (
                expected[group]
            ):
                raise ValueError(
                    'equipment evidence subject diverges from the bound '
                    'EquipmentDefinition'
                )

    def resolve_definition_evidence(
        self,
        definition: EquipmentDefinition,
    ) -> ResolvedEquipmentEvidence:
        """Re-resolve and re-derive every evidence claim on a definition.

        Every cited ``EquipmentDataProvenance`` must resolve to a retained
        authority bound to this exact definition whose recorded subject
        reproduces the normalized values it is attached to; the
        definition-level claims must jointly support the structural groups.
        Missing, foreign or divergent evidence fails closed.
        """
        definition = EquipmentDefinition.model_validate(
            definition.model_dump(mode='python')
        )
        expected = equipment_field_group_subjects(definition)
        resolved: dict[str, EquipmentEvidenceAuthority] = {}
        for provenance in equipment_provenance_items(definition):
            evidence = self._resolve_provenance_authority(
                definition.semantic_sha256,
                provenance,
            )
            resolved[provenance_sha256(provenance)] = evidence

        # Anti-divergence: a retained subject may only assert values the
        # bound definition actually carries.
        for evidence in resolved.values():
            self._assert_subject_consistent(evidence.subject, definition)

        def evidence_for(
            provenance: EquipmentDataProvenance,
        ) -> EquipmentEvidenceAuthority:
            return resolved[provenance_sha256(provenance)]

        def require_group(
            evidence: EquipmentEvidenceAuthority,
            group: EquipmentEvidenceFieldGroup,
        ) -> None:
            if group not in evidence.subject.supported_groups():
                raise ValueError(
                    f'equipment evidence {evidence.evidence_id} does not '
                    f'support field group {group!r}'
                )

        # Definition-level claims jointly cover the structural field groups.
        definition_level = [
            evidence_for(item) for item in definition.provenance
        ]
        for group in EQUIPMENT_STRUCTURAL_FIELD_GROUPS:
            if not any(
                group in evidence.subject.supported_groups()
                for evidence in definition_level
            ):
                raise ValueError(
                    f'no retained equipment evidence supports field group '
                    f'{group!r}'
                )

        if definition.sensitivity is not None:
            require_group(
                evidence_for(definition.sensitivity.provenance),
                'sensitivity_reference',
            )
        if definition.spl_capability is not None:
            require_group(
                evidence_for(definition.spl_capability.provenance),
                'spl_capability',
            )
        for item in definition.uncertainty:
            uncertainty_evidence = evidence_for(item.provenance)
            stripped = EquipmentUncertaintySubject.from_item(item)
            if stripped not in uncertainty_evidence.subject.uncertainty:
                raise ValueError(
                    'equipment uncertainty item is not supported by the '
                    'retained evidence for its provenance claim'
                )
        require_group(
            evidence_for(definition.directivity.provenance),
            'directivity_capability',
        )
        if definition.directivity.interpolation is not None:
            require_group(
                evidence_for(
                    definition.directivity.interpolation.provenance
                ),
                'directivity_interpolation',
            )

        field_refs: list[EquipmentFieldEvidenceRef] = []
        for group in (
            'identity',
            'cabinet_geometry',
            'installation',
            'sensitivity_reference',
            'spl_capability',
            'uncertainty',
            'directivity_capability',
            'directivity_interpolation',
        ):
            if group not in expected:
                continue
            for evidence in resolved.values():
                if group in evidence.subject.supported_groups():
                    field_refs.append(
                        EquipmentFieldEvidenceRef(
                            field_group=group,  # type: ignore[arg-type]
                            evidence_id=evidence.evidence_id,
                            evidence_sha256=evidence.evidence_sha256,
                        )
                    )
        return ResolvedEquipmentEvidence(
            equipment_definition_id=definition.definition_id,
            equipment_definition_version=definition.version,
            equipment_definition_sha256=definition.semantic_sha256,
            field_evidence=tuple(field_refs),
            authorities=tuple(resolved.values()),
        )

    # ------------------------------------------------------------------
    # EquipmentDefinition persistence (evidence-gated)

    def _definition_row_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentDefinition | None:
        """Raw payload read without evidence resolution (internal use)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentDefinition.model_validate_json(row['payload_json'])
        )

    def save_definition(
        self,
        definition: EquipmentDefinition,
    ) -> EquipmentDefinition:
        definition = EquipmentDefinition.model_validate(
            definition.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE definition_id=? AND version=?
                """,
                (definition.definition_id, definition.version),
            ).fetchone()
            if existing is not None:
                persisted = EquipmentDefinition.model_validate_json(
                    existing['payload_json']
                )
                if persisted != definition:
                    raise ValueError(
                        'equipment definition id/version already exists with different semantics'
                    )
                # Re-resolve and re-derive the retained evidence on every
                # save, even for an idempotent rewrite.
                self.resolve_definition_evidence(persisted)
                return persisted
            # A new definition is only persisted once every cited provenance
            # claim resolves to retained evidence whose subject reproduces
            # its normalized values.
            self.resolve_definition_evidence(definition)
            connection.execute(
                """
                INSERT INTO cad_equipment_definitions(
                    definition_id, version, semantic_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.version,
                    definition.semantic_sha256,
                    definition.model_dump_json(),
                    _utc_now(),
                ),
            )
        return definition

    def get_definition(
        self,
        definition_id: str,
        version: str,
    ) -> EquipmentDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE definition_id=? AND version=?
                """,
                (definition_id, version),
            ).fetchone()
        if row is None:
            return None
        definition = EquipmentDefinition.model_validate_json(
            row['payload_json']
        )
        self.resolve_definition_evidence(definition)
        return definition

    def get_definition_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        definition = EquipmentDefinition.model_validate_json(
            row['payload_json']
        )
        self.resolve_definition_evidence(definition)
        return definition

    def catalog_snapshot(self) -> EquipmentCatalogSnapshot:
        return build_equipment_catalog_snapshot(
            self.list_definitions()
        )

    def list_definitions(self) -> tuple[EquipmentDefinition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                ORDER BY seq ASC
                """
            ).fetchall()
        definitions = tuple(
            EquipmentDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )
        for definition in definitions:
            self.resolve_definition_evidence(definition)
        return definitions

    def evaluate_capability(
        self,
        definition: EquipmentDefinition,
        claim: EquipmentCapabilityClaim,
        *,
        frequency_hz: float | None = None,
        horizontal_angle_deg: float | None = None,
        vertical_angle_deg: float | None = None,
    ) -> EquipmentCapabilityResult:
        """Evidence-gated capability claim evaluation.

        Every cited provenance must resolve to retained evidence whose
        subject reproduces the definition before the field-presence gate
        runs, so production sensitivity/SPL/headroom claims can never be
        authorized by a bare ``source_sha256``.
        """
        self.resolve_definition_evidence(definition)
        return evaluate_equipment_capability(
            definition,
            claim,
            frequency_hz=frequency_hz,
            horizontal_angle_deg=horizontal_angle_deg,
            vertical_angle_deg=vertical_angle_deg,
        )

    def capability_matrix(
        self,
        definition: EquipmentDefinition,
        *,
        frequency_hz: float | None = None,
        horizontal_angle_deg: float | None = None,
        vertical_angle_deg: float | None = None,
    ) -> tuple[EquipmentCapabilityResult, ...]:
        """Evidence-gated full capability matrix."""
        self.resolve_definition_evidence(definition)
        return equipment_capability_matrix(
            definition,
            frequency_hz=frequency_hz,
            horizontal_angle_deg=horizontal_angle_deg,
            vertical_angle_deg=vertical_angle_deg,
        )

    def _definition_for_ref(
        self,
        binding: EquipmentBindingRef,
    ) -> EquipmentDefinition:
        definition = self.get_definition_by_hash(
            binding.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'SystemVariant equipment binding references an unpersisted definition'
            )
        if (
            definition.definition_id != binding.equipment_definition_id
            or definition.version != binding.equipment_definition_version
        ):
            raise ValueError(
                'SystemVariant equipment binding definition identity mismatch'
            )
        return definition

    def resolve_variant_bindings(
        self,
        variant_id: str,
    ) -> tuple[ResolvedEquipmentBinding, ...]:
        if self.variant_repository is None:
            raise ValueError(
                'variant equipment resolution requires CadSystemVariantRepository'
            )
        variant = self.variant_repository.get_variant(variant_id)
        if variant is None:
            raise ValueError('SystemVariant does not exist')
        return self.resolve_bindings(variant)

    def resolve_bindings(
        self,
        variant: SystemVariant,
    ) -> tuple[ResolvedEquipmentBinding, ...]:
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        if (
            baseline.document_id != variant.document_id
            or baseline.content_hash != variant.baseline_content_hash
        ):
            raise ValueError('SystemVariant baseline authority mismatch')

        scene = materialize_system_variant(baseline, variant)
        final_entities = {
            entity.entity_id: entity
            for entity in scene.entities
        }

        resolved: list[ResolvedEquipmentBinding] = []
        for binding in variant.equipment_bindings:
            entity = final_entities.get(binding.entity_id)
            if entity is None:
                raise ValueError(
                    'SystemVariant equipment binding references missing source entity'
                )
            if entity.kind != 'speaker':
                raise ValueError(
                    'SystemVariant equipment binding requires a speaker source entity'
                )
            definition = self._definition_for_ref(binding)
            resolved.append(
                ResolvedEquipmentBinding(
                    variant_id=variant.variant_id,
                    variant_sha256=variant.variant_sha256,
                    entity_id=binding.entity_id,
                    binding=binding,
                    definition=definition,
                )
            )
        return tuple(resolved)

    def definition_for_variant_entity(
        self,
        variant_id: str,
        entity_id: str,
    ) -> EquipmentDefinition:
        matches = [
            item.definition
            for item in self.resolve_variant_bindings(variant_id)
            if item.entity_id == entity_id
        ]
        if len(matches) != 1:
            raise ValueError(
                'exactly one persisted equipment binding is required for source entity'
            )
        return matches[0]
