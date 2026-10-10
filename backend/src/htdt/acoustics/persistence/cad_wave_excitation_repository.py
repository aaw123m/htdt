
import sqlite3
from contextlib import (
    closing,
)
from hashlib import (
    sha256,
)
from pathlib import (
    Path,
)
from ...cad_equipment_repository import (
    CadEquipmentRepository,
)
from ...cad_r110_source import (
    R110CompiledSourceModel,
)
from ...cad_r110_source_repository import (
    CadR110SourceRepository,
)
from ...cad_repository import (
    SceneRepository,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...cad_source_response import (
    CadSourceResponseRepository,
)
from ...clock import (
    utc_now_iso as _utc_now,
)
from ...managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetStore,
)
from ...r120_geometry_compiler import (
    ExactExternalAuthorityRef,
)
from ..domain.cad_wave_excitation import (
    AcousticWaveExcitationAuthority,
    ComplexVolumeVelocitySample,
    WAVE_EXCITATION_EVIDENCE_ID_PREFIX,
    WaveExcitationAnalyticDerivation,
    WaveExcitationEvidenceAuthority,
    WaveExcitationSourceAssetDerivation,
    WaveExcitationSourceAssetMetadata,
    WaveExcitationSourceResponseDerivation,
    WaveSourceExcitationBinding,
    bind_wave_excitation_to_r110_source,
    replay_wave_excitation_analytic_derivation,
    replay_wave_excitation_source_derivation,
)

class CadWaveExcitationRepository:
    """Append-only explicit acoustic excitation and R110-binding persistence.

    Every excitation provenance claim is paired with a typed ref to a retained
    ``WaveExcitationEvidenceAuthority``. On save and on every read the evidence
    must resolve, its provenance/subject must match the excitation exactly, and
    the recorded derivation must replay to the persisted samples — measured /
    manufacturer / inferred evidence re-opens its managed source asset and
    re-runs the pinned converter, analytic evidence regenerates the recorded
    model, and manual evidence is the explicit retained statement. Missing,
    tampered, unregistered-converter or non-replaying evidence fails closed.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        equipment_repository: CadEquipmentRepository | None = None,
        r110_repository: CadR110SourceRepository | None = None,
        source_response_repository: CadSourceResponseRepository | None = None,
        assets_dir: Path | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.equipment_repository = (
            equipment_repository
            if equipment_repository is not None
            else CadEquipmentRepository(scene_repository)
        )
        self.r110_repository = (
            r110_repository
            if r110_repository is not None
            else CadR110SourceRepository(
                scene_repository,
                equipment_repository=self.equipment_repository,
            )
        )
        self.source_response_repository = (
            source_response_repository
            if source_response_repository is not None
            else CadSourceResponseRepository(
                scene_repository.path,
                equipment_repository=self.equipment_repository,
            )
        )
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('Equipment', self.equipment_repository),
            ('R110', self.r110_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'wave excitation and {label} repositories must share '
                    'one native CAD database'
                )
        # Resolve any interrupted managed-data restore before the asset store
        # can create its directory inside the managed data root.
        ensure_native_schema(self.path)
        self.assets_dir = (
            Path(assets_dir)
            if assets_dir is not None
            else self.path.parent / MANAGED_ASSETS_DIRNAME
        )
        self._asset_store = ManagedAssetStore(self.assets_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_acoustic_wave_excitations', 'cad_wave_source_excitation_bindings', 'cad_measurement_assets', 'cad_wave_excitation_source_assets', 'cad_wave_excitation_evidence_authorities')

    def _verified_asset_file(
        self,
        digest: str,
        *,
        relative_path: str,
        size_bytes: int,
    ) -> bytes:
        """Reopen a registered managed asset, failing closed on tampering."""
        root = self.path.parent.resolve()
        target = (self.path.parent / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                'managed excitation source asset escapes the data root'
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError('managed excitation source asset is missing')
        if target.stat().st_size != size_bytes:
            raise ValueError('managed excitation source asset size mismatch')
        raw = self._asset_store.read_file(target)
        if sha256(raw).hexdigest() != digest:
            raise ValueError(
                'managed excitation source asset SHA-256 mismatch'
            )
        return raw

    def _verified_source_asset(self, digest: str) -> bytes:
        """Reopen the exact bound source bytes for *digest*.

        Evidence whose source asset is not registered, or whose managed file
        is missing or fails the size/SHA-256 contract, fails closed — the
        excitation is never served without its exact evidence.
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
                'wave-excitation source asset is not registered in the '
                'managed asset store'
            )
        return self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def read_source_asset(self, source_asset_sha256: str) -> bytes | None:
        """Reopen the exact original source bytes for a content address.

        Returns ``None`` only when no managed asset is registered for the
        digest; a registered asset whose file is missing, resized or tampered
        raises instead of returning unverifiable bytes.
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

    def get_source_metadata(
        self,
        source_asset_sha256: str,
    ) -> WaveExcitationSourceAssetMetadata | None:
        """Return preserved filename/media/schema metadata for an asset.

        The bound managed file is re-verified before metadata is served, so
        tampered evidence never comes back with clean provenance.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT a.filename, a.media_type, a.declared_schema,
                       a.recorded_at_utc,
                       m.size_bytes, m.relative_path
                FROM cad_wave_excitation_source_assets a
                JOIN cad_measurement_assets m
                    ON m.sha256 = a.source_asset_sha256
                WHERE a.source_asset_sha256=?
                """,
                (source_asset_sha256,),
            ).fetchone()
        if row is None:
            return None
        self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )
        return WaveExcitationSourceAssetMetadata(
            source_asset_sha256=source_asset_sha256,
            filename=row['filename'],
            media_type=row['media_type'],
            declared_schema=row['declared_schema'],
            size_bytes=int(row['size_bytes']),
            recorded_at_utc=row['recorded_at_utc'],
        )

    def _replay_evidence(
        self,
        evidence: WaveExcitationEvidenceAuthority,
        *,
        source_bytes: bytes | None = None,
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        """Reproduce the exact samples a derivation produces.

        Source-asset evidence re-opens the managed bytes (or uses supplied
        bytes already verified against the content address) and re-runs the
        pinned converter; analytic evidence regenerates the recorded model;
        manual evidence is the retained statement itself.
        """
        derivation = evidence.derivation
        if isinstance(derivation, WaveExcitationSourceResponseDerivation):
            response = self.source_response_repository.get_response_by_sha256(
                derivation.source_response_sha256
            )
            if response is None:
                raise ValueError(
                    'wave-excitation source-response authority is not '
                    'persisted: cannot replay derivation'
                )
            if (
                response.response_id != derivation.source_response_id
                or response.authority_version
                != derivation.source_response_version
            ):
                raise ValueError(
                    'wave-excitation source-response identity does not match '
                    'the persisted authority'
                )
            return replay_wave_excitation_source_derivation(
                source_bytes=response.model_dump_json().encode('utf-8'),
                converter_id=derivation.converter_id,
                converter_version=derivation.converter_version,
                conversion_parameters=derivation.conversion_parameters,
            )
        if isinstance(derivation, WaveExcitationSourceAssetDerivation):
            bound_source = (
                source_bytes
                if source_bytes is not None
                else self._verified_source_asset(derivation.source_asset_sha256)
            )
            return replay_wave_excitation_source_derivation(
                source_bytes=bound_source,
                converter_id=derivation.converter_id,
                converter_version=derivation.converter_version,
                conversion_parameters=derivation.conversion_parameters,
            )
        if isinstance(derivation, WaveExcitationAnalyticDerivation):
            return replay_wave_excitation_analytic_derivation(
                model_id=derivation.model_id,
                model_version=derivation.model_version,
                model_parameters=derivation.model_parameters,
            )
        return tuple(evidence.subject.samples)

    def _replay_verified_evidence(
        self,
        evidence: WaveExcitationEvidenceAuthority,
        *,
        source_bytes: bytes | None = None,
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        """Re-run the recorded derivation; it must equal the subject exactly."""
        replayed = self._replay_evidence(evidence, source_bytes=source_bytes)
        if tuple(replayed) != tuple(evidence.subject.samples):
            raise ValueError(
                'wave-excitation evidence does not replay to its retained '
                'subject samples'
            )
        return replayed

    def save_evidence(
        self,
        evidence: WaveExcitationEvidenceAuthority,
        *,
        source_bytes: bytes | None = None,
        source_filename: str | None = None,
        media_type: str | None = None,
        declared_schema: str | None = None,
    ) -> WaveExcitationEvidenceAuthority:
        """Persist an immutable excitation evidence authority.

        For source-asset evidence, ``source_bytes`` are the exact imported
        bytes the subject was derived from: they must hash to
        ``derivation.source_asset_sha256``, are installed atomically into the
        managed asset store, and the recorded converter must replay the subject
        samples from them before anything is committed. When ``source_bytes``
        is omitted the evidence must bind to an already-managed asset — it is
        re-verified and replayed the same way — so evidence can never be
        persisted as a hash without retained bytes behind it.
        """
        evidence = WaveExcitationEvidenceAuthority.model_validate(
            evidence.model_dump(mode='python')
        )
        derivation = evidence.derivation
        bound_source: bytes | None = None
        if isinstance(derivation, WaveExcitationSourceAssetDerivation):
            digest = derivation.source_asset_sha256
            if source_bytes is not None:
                if not isinstance(source_bytes, bytes):
                    raise TypeError(
                        'wave-excitation source asset payload must be bytes'
                    )
                if sha256(source_bytes).hexdigest() != digest:
                    raise ValueError(
                        'wave-excitation source bytes do not match '
                        'derivation.source_asset_sha256'
                    )
                if not source_filename:
                    raise ValueError(
                        'wave-excitation source filename is required when '
                        'persisting source bytes'
                    )
                bound_source = source_bytes
            else:
                bound_source = self._verified_source_asset(digest)
            self._replay_verified_evidence(
                evidence, source_bytes=bound_source
            )
        else:
            if source_bytes is not None:
                raise ValueError(
                    'wave-excitation source bytes are only valid for '
                    'source-asset evidence'
                )
            self._replay_verified_evidence(evidence)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_wave_excitation_evidence_authorities
                WHERE evidence_id=?
                """,
                (evidence.evidence_id,),
            ).fetchone()
            persisted: WaveExcitationEvidenceAuthority | None = None
            if existing is not None:
                persisted = WaveExcitationEvidenceAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evidence:
                    raise ValueError(
                        'wave-excitation evidence id exists with different '
                        'semantics'
                    )
            if isinstance(derivation, WaveExcitationSourceAssetDerivation):
                # Install under the same write exclusion as the row inserts
                # so the republish is atomic against storage GC's delete
                # window; a failed commit leaves a safe content-addressed
                # orphan rather than a partially written file.
                if bound_source is not None:
                    self._asset_store.ensure_installed(
                        derivation.source_asset_sha256, bound_source
                    )
                target = self._asset_store.asset_path(
                    derivation.source_asset_sha256
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        derivation.source_asset_sha256,
                        source_filename or '',
                        target.relative_to(self.path.parent).as_posix(),
                        len(bound_source) if bound_source is not None else 0,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cad_wave_excitation_source_assets(
                        source_asset_sha256, filename, media_type,
                        declared_schema, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        derivation.source_asset_sha256,
                        source_filename,
                        media_type,
                        declared_schema,
                        _utc_now(),
                    ),
                )
            if persisted is None:
                connection.execute(
                    """
                    INSERT INTO cad_wave_excitation_evidence_authorities(
                        evidence_id,
                        evidence_sha256,
                        evidence_kind,
                        source_sha256,
                        subject_sha256,
                        payload_json,
                        recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.evidence_id,
                        evidence.evidence_sha256,
                        evidence.provenance.evidence_kind,
                        evidence.provenance.source_sha256,
                        evidence.subject_sha256(),
                        evidence.model_dump_json(),
                        _utc_now(),
                    ),
                )
        return persisted if persisted is not None else evidence

    def get_evidence(
        self,
        evidence_id: str,
    ) -> WaveExcitationEvidenceAuthority | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evidence_sha256, payload_json
                FROM cad_wave_excitation_evidence_authorities
                WHERE evidence_id=?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = WaveExcitationEvidenceAuthority.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != evidence_id
            or evidence.evidence_sha256 != row['evidence_sha256']
        ):
            raise ValueError(
                'persisted wave-excitation evidence authority identity mismatch'
            )
        self._replay_verified_evidence(evidence)
        return evidence

    def resolve_evidence(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> WaveExcitationEvidenceAuthority | None:
        """Resolve an exact authority ref; tampered payloads fail closed."""
        if not ref.authority_id.startswith(WAVE_EXCITATION_EVIDENCE_ID_PREFIX):
            return None
        evidence = self.get_evidence(ref.authority_id)
        if evidence is None or evidence.as_external_ref() != ref:
            return None
        return evidence

    def _resolve_excitation_evidence(
        self,
        excitation: AcousticWaveExcitationAuthority,
    ) -> tuple[WaveExcitationEvidenceAuthority, ...]:
        """Resolve and re-verify every evidence ref an excitation claims.

        Each provenance entry is paired positionally with a typed ref that must
        resolve to a retained evidence authority whose provenance matches the
        claim exactly and whose subject supports the persisted definition and
        samples; the recorded derivation must replay to the persisted samples.
        """
        resolved: list[WaveExcitationEvidenceAuthority] = []
        for claim, ref in zip(
            excitation.provenance, excitation.source_evidence, strict=True
        ):
            evidence = self.resolve_evidence(ref)
            if evidence is None:
                raise ValueError(
                    'wave excitation source evidence does not resolve to a '
                    'retained evidence authority'
                )
            if evidence.provenance != claim:
                raise ValueError(
                    'wave excitation provenance claim does not match the '
                    'retained evidence authority'
                )
            if (
                evidence.subject.definition_id != excitation.definition_id
                or evidence.subject.definition_version
                != excitation.definition_version
                or evidence.subject.definition_sha256
                != excitation.definition_sha256
            ):
                raise ValueError(
                    'wave excitation evidence subject does not match the '
                    'excitation EquipmentDefinition'
                )
            if tuple(evidence.subject.samples) != tuple(excitation.samples):
                raise ValueError(
                    'wave excitation evidence subject does not support the '
                    'persisted samples'
                )
            replayed = self._replay_verified_evidence(evidence)
            if tuple(replayed) != tuple(excitation.samples):
                raise ValueError(
                    'wave excitation does not replay from its retained '
                    'source evidence'
                )
            resolved.append(evidence)
        if not any(
            item.provenance == excitation.interpolation.provenance
            for item in resolved
        ):
            raise ValueError(
                'wave excitation interpolation provenance does not resolve '
                'to a retained evidence item'
            )
        return tuple(resolved)

    def _validate_excitation(
        self,
        excitation: AcousticWaveExcitationAuthority,
    ) -> AcousticWaveExcitationAuthority:
        excitation = AcousticWaveExcitationAuthority.model_validate(
            excitation.model_dump(mode='python')
        )
        definition = self.equipment_repository.get_definition_by_hash(
            excitation.definition_sha256
        )
        if definition is None:
            raise ValueError(
                'wave excitation references missing EquipmentDefinition'
            )
        if (
            definition.definition_id != excitation.definition_id
            or definition.version != excitation.definition_version
        ):
            raise ValueError('wave excitation EquipmentDefinition identity mismatch')
        self._resolve_excitation_evidence(excitation)
        return excitation

    def save_excitation(
        self,
        excitation: AcousticWaveExcitationAuthority,
    ) -> AcousticWaveExcitationAuthority:
        excitation = self._validate_excitation(excitation)
        persisted: AcousticWaveExcitationAuthority | None = None
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_wave_excitations
                WHERE excitation_id=?
                """,
                (excitation.excitation_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticWaveExcitationAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != excitation:
                    raise ValueError(
                        'wave excitation id exists with different semantics'
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO cad_acoustic_wave_excitations(
                        excitation_id,
                        semantic_sha256,
                        equipment_definition_sha256,
                        payload_json,
                        recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        excitation.excitation_id,
                        excitation.semantic_sha256,
                        excitation.definition_sha256,
                        excitation.model_dump_json(),
                        _utc_now(),
                    ),
                )
        # Re-validation opens its own connections; it must run after the
        # write transaction commits rather than under its writer lock.
        if persisted is not None:
            return self._validate_excitation(persisted)
        return excitation

    def _decode_excitation_row(
        self,
        row: sqlite3.Row,
    ) -> AcousticWaveExcitationAuthority:
        excitation = AcousticWaveExcitationAuthority.model_validate_json(
            row['payload_json']
        )
        if (
            excitation.excitation_id != row['excitation_id']
            or excitation.semantic_sha256 != row['semantic_sha256']
        ):
            raise ValueError(
                'persisted wave excitation identity columns do not match '
                'its payload'
            )
        return self._validate_excitation(excitation)

    def get_excitation(
        self,
        excitation_id: str,
    ) -> AcousticWaveExcitationAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT excitation_id, semantic_sha256, payload_json
                FROM cad_acoustic_wave_excitations
                WHERE excitation_id=?
                """,
                (excitation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_excitation_row(row)

    def get_excitation_by_hash(
        self,
        semantic_sha256: str,
    ) -> AcousticWaveExcitationAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT excitation_id, semantic_sha256, payload_json
                FROM cad_acoustic_wave_excitations
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_excitation_row(row)

    def _validate_binding(
        self,
        binding: WaveSourceExcitationBinding,
    ) -> WaveSourceExcitationBinding:
        binding = WaveSourceExcitationBinding.model_validate(
            binding.model_dump(mode='python')
        )
        source = self.r110_repository.get_model(
            binding.r110_compiled_source_sha256
        )
        if source is None:
            raise ValueError(
                'wave source binding references missing R110CompiledSourceModel'
            )
        excitation = self.get_excitation(binding.excitation_id)
        if excitation is None:
            raise ValueError(
                'wave source binding references missing excitation authority'
            )
        if excitation.semantic_sha256 != binding.excitation_semantic_sha256:
            raise ValueError('wave source binding excitation hash mismatch')
        recomputed = bind_wave_excitation_to_r110_source(
            source=source,
            excitation=excitation,
        )
        if recomputed != binding:
            raise ValueError(
                'wave source binding does not reproduce from exact authorities'
            )
        return binding

    def save_binding(
        self,
        binding: WaveSourceExcitationBinding,
    ) -> WaveSourceExcitationBinding:
        binding = self._validate_binding(binding)
        persisted: WaveSourceExcitationBinding | None = None
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_wave_source_excitation_bindings
                WHERE binding_id=?
                """,
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = WaveSourceExcitationBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted != binding:
                    raise ValueError(
                        'wave source binding id exists with different semantics'
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO cad_wave_source_excitation_bindings(
                        binding_id,
                        semantic_sha256,
                        r110_compiled_source_sha256,
                        excitation_id,
                        excitation_semantic_sha256,
                        payload_json,
                        recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.binding_id,
                        binding.semantic_sha256,
                        binding.r110_compiled_source_sha256,
                        binding.excitation_id,
                        binding.excitation_semantic_sha256,
                        binding.model_dump_json(),
                        _utc_now(),
                    ),
                )
        # Re-validation opens its own connections; it must run after the
        # write transaction commits rather than under its writer lock.
        if persisted is not None:
            return self._validate_binding(persisted)
        return binding

    def get_binding(
        self,
        binding_id: str,
    ) -> WaveSourceExcitationBinding | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_wave_source_excitation_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_binding(
            WaveSourceExcitationBinding.model_validate_json(
                row['payload_json']
            )
        )

__all__ = [
    'CadWaveExcitationRepository',
]
