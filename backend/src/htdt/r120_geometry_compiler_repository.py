from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import NamedTuple

from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .r120_geometry_compiler import (
    AcousticRegionAuthority,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
    R120GeometryCompilationError,
    R120LeakPortalDiagnostic,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
    diagnose_r120_leak_and_portals,
)
from .canonical_json import canonical_json


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CompileInputAuthorities(NamedTuple):
    """Exact non-scene inputs the canonical R120 compiler consumed."""

    surface_boundary_bindings: tuple[SurfaceBoundaryAuthorityBinding, ...]
    region_authority: AcousticRegionAuthority | None
    portal_authority: PortalAuthority | None
    boundary_termination_authority: BoundaryTerminationAuthority | None


class _DiagnosticInputAuthorities(NamedTuple):
    """Exact non-compiled inputs the canonical leak/portal diagnostic consumed."""

    portal_authority: PortalAuthority | None


def _authority_ref_of(authority: object) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority.authority_id,
        authority_version=authority.authority_version,
        semantic_hash_sha256=authority.semantic_hash_sha256,
    )


def _compile_inputs_to_json(inputs: _CompileInputAuthorities) -> str:
    def authority_payload(authority: object) -> object:
        return (
            None
            if authority is None
            else authority.model_dump(mode='json')
        )

    return json.dumps(
        {
            'surface_boundary_bindings': [
                item.model_dump(mode='json')
                for item in inputs.surface_boundary_bindings
            ],
            'region_authority': authority_payload(inputs.region_authority),
            'portal_authority': authority_payload(inputs.portal_authority),
            'boundary_termination_authority': authority_payload(
                inputs.boundary_termination_authority
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _compile_inputs_from_json(payload: str) -> _CompileInputAuthorities:
    raw = json.loads(payload)

    def authority(model_type: type, key: str) -> object:
        value = raw.get(key)
        return None if value is None else model_type.model_validate(value)

    return _CompileInputAuthorities(
        surface_boundary_bindings=tuple(
            SurfaceBoundaryAuthorityBinding.model_validate(item)
            for item in raw['surface_boundary_bindings']
        ),
        region_authority=authority(AcousticRegionAuthority, 'region_authority'),
        portal_authority=authority(PortalAuthority, 'portal_authority'),
        boundary_termination_authority=authority(
            BoundaryTerminationAuthority,
            'boundary_termination_authority',
        ),
    )


def _diagnostic_inputs_to_json(
    portal_authority: PortalAuthority | None,
) -> str:
    return canonical_json({
            'portal_authority': (
                None
                if portal_authority is None
                else portal_authority.model_dump(mode='json')
            ),
        })


def _diagnostic_inputs_from_json(payload: str) -> _DiagnosticInputAuthorities:
    raw = json.loads(payload)
    value = raw.get('portal_authority')
    return _DiagnosticInputAuthorities(
        portal_authority=(
            None if value is None else PortalAuthority.model_validate(value)
        )
    )


class R120GeometryCompilerRepository:
    """Append-only persistence for solver-neutral R120 compiler authorities.

    Model validity and self-hashes are necessary but not sufficient for a
    persisted R120 row. Every write and every authoritative read re-establishes
    canonical derivation from the retained exact input authorities:

    * ``save_compiled_geometry`` / ``get_compiled_geometry`` /
      ``get_compiled_geometry_by_hash`` resolve the exact ``SceneRevision`` and
      ``SemanticAcousticGeometry`` bindings, resolve every non-null
      region/portal/termination authority ref to its retained exact authority,
      recover the retained ``surface_boundary_bindings``, then re-run
      ``compile_r120_geometry(...)`` and require exact equality with the stored
      compiled output;
    * ``save_leak_portal_diagnostic`` / ``get_leak_portal_diagnostic`` resolve
      the exact compiled geometry (itself re-derived), resolve the retained
      ``PortalAuthority`` the diagnostic consumed, then re-run
      ``diagnose_r120_leak_and_portals(...)`` and require exact equality.

    Compile inputs that the compiled payload does not fully retain are stored
    in ``cad_r120_compile_inputs`` / ``cad_r120_leak_diagnostic_inputs`` at save
    time; a missing retained input or a tampered derived field fails closed.
    ``surface_boundary_bindings`` are recoverable verbatim from the persisted
    surface mapping (the compiler copies the material/boundary-physics refs it
    consumes), so they may be supplied explicitly or derived at first save and
    are retained either way so later passthrough tampering cannot re-derive a
    divergent input set.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_r120_compiled_geometry', 'cad_r120_compile_inputs', 'cad_r120_leak_portal_diagnostics', 'cad_r120_leak_diagnostic_inputs')

    def save_compiled_geometry(
        self,
        compiled: R120CompiledGeometry,
        *,
        surface_boundary_bindings: tuple[
            SurfaceBoundaryAuthorityBinding, ...
        ] | None = None,
        region_authority: AcousticRegionAuthority | None = None,
        portal_authority: PortalAuthority | None = None,
        boundary_termination_authority: BoundaryTerminationAuthority | None = None,
    ) -> R120CompiledGeometry:
        compiled = R120CompiledGeometry.model_validate(
            compiled.model_dump(mode='python')
        )
        revision = self._validate_scene_binding(compiled)
        retained = self._retained_compile_inputs(compiled)
        inputs = self._resolve_compile_inputs(
            compiled,
            surface_boundary_bindings=surface_boundary_bindings,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=boundary_termination_authority,
            retained=retained,
        )
        self._require_canonical_recompile(compiled, revision, inputs)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_r120_compiled_geometry
                WHERE compiled_geometry_id=?
                """,
                (compiled.compiled_geometry_id,),
            ).fetchone()
            if existing is not None:
                persisted = R120CompiledGeometry.model_validate_json(
                    existing['payload_json']
                )
                if persisted != compiled:
                    raise ValueError(
                        'compiled geometry id already exists with different semantics'
                    )
                if retained is None:
                    # Legacy/corrupt row missing its retained inputs: repair it
                    # with the inputs that just reproduced the payload exactly.
                    self._persist_compile_inputs(connection, compiled, inputs)
                return persisted

            hash_collision = connection.execute(
                """
                SELECT payload_json
                FROM cad_r120_compiled_geometry
                WHERE compiled_hash_sha256=?
                """,
                (compiled.compiled_hash_sha256,),
            ).fetchone()
            if hash_collision is not None:
                persisted = R120CompiledGeometry.model_validate_json(
                    hash_collision['payload_json']
                )
                if persisted != compiled:
                    raise ValueError(
                        'compiled geometry hash already exists with different semantics'
                    )
                if retained is None:
                    self._persist_compile_inputs(connection, compiled, inputs)
                return persisted

            connection.execute(
                """
                INSERT INTO cad_r120_compiled_geometry(
                    compiled_geometry_id,
                    compiled_hash_sha256,
                    scene_revision_id,
                    scene_revision_content_hash,
                    semantic_geometry_id,
                    semantic_geometry_hash_sha256,
                    request_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compiled.compiled_geometry_id,
                    compiled.compiled_hash_sha256,
                    compiled.exact_scene_revision_id,
                    compiled.exact_scene_revision_content_hash,
                    compiled.exact_semantic_geometry_id,
                    compiled.exact_semantic_geometry_hash_sha256,
                    compiled.request.request_id,
                    compiled.model_dump_json(),
                    _utc_now(),
                ),
            )
            if retained is None:
                self._persist_compile_inputs(connection, compiled, inputs)
        return compiled

    def get_compiled_geometry(
        self,
        compiled_geometry_id: str,
    ) -> R120CompiledGeometry | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT compiled_geometry_id, compiled_hash_sha256, payload_json
                FROM cad_r120_compiled_geometry
                WHERE compiled_geometry_id=?
                """,
                (compiled_geometry_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validated_compiled_row(row)

    def get_compiled_geometry_by_hash(
        self,
        compiled_hash_sha256: str,
    ) -> R120CompiledGeometry | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT compiled_geometry_id, compiled_hash_sha256, payload_json
                FROM cad_r120_compiled_geometry
                WHERE compiled_hash_sha256=?
                """,
                (compiled_hash_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._validated_compiled_row(row)

    def save_leak_portal_diagnostic(
        self,
        diagnostic: R120LeakPortalDiagnostic,
        *,
        portal_authority: PortalAuthority | None = None,
    ) -> R120LeakPortalDiagnostic:
        diagnostic = R120LeakPortalDiagnostic.model_validate(
            diagnostic.model_dump(mode='python')
        )
        compiled = self.get_compiled_geometry(
            diagnostic.exact_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError(
                'leak/portal diagnostic references unpersisted compiled geometry'
            )
        if (
            compiled.compiled_hash_sha256
            != diagnostic.exact_compiled_geometry_hash_sha256
        ):
            raise ValueError(
                'leak/portal diagnostic compiled geometry hash mismatch'
            )

        retained = self._retained_diagnostic_inputs(diagnostic)
        portal = self._resolve_input_authority(
            ref=diagnostic.portal_authority_ref,
            supplied=portal_authority,
            retained=None if retained is None else retained.portal_authority,
            expected_type=PortalAuthority,
            artifact='leak/portal diagnostic',
            label='portal',
        )
        self._require_canonical_rediagnose(diagnostic, compiled, portal)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_r120_leak_portal_diagnostics
                WHERE diagnostic_result_id=?
                """,
                (diagnostic.diagnostic_result_id,),
            ).fetchone()
            if existing is not None:
                persisted = R120LeakPortalDiagnostic.model_validate_json(
                    existing['payload_json']
                )
                if persisted != diagnostic:
                    raise ValueError(
                        'leak/portal diagnostic id already exists with different semantics'
                    )
                if retained is None:
                    # Legacy/corrupt row missing its retained inputs: repair it
                    # with the inputs that just reproduced the payload exactly.
                    self._persist_diagnostic_inputs(
                        connection, diagnostic, portal
                    )
                return persisted

            hash_collision = connection.execute(
                """
                SELECT payload_json
                FROM cad_r120_leak_portal_diagnostics
                WHERE diagnostic_hash_sha256=?
                """,
                (diagnostic.diagnostic_hash_sha256,),
            ).fetchone()
            if hash_collision is not None:
                persisted = R120LeakPortalDiagnostic.model_validate_json(
                    hash_collision['payload_json']
                )
                if persisted != diagnostic:
                    raise ValueError(
                        'leak/portal diagnostic hash already exists with different semantics'
                    )
                if retained is None:
                    self._persist_diagnostic_inputs(
                        connection, diagnostic, portal
                    )
                return persisted

            connection.execute(
                """
                INSERT INTO cad_r120_leak_portal_diagnostics(
                    diagnostic_result_id,
                    diagnostic_hash_sha256,
                    compiled_geometry_id,
                    compiled_geometry_hash_sha256,
                    request_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnostic.diagnostic_result_id,
                    diagnostic.diagnostic_hash_sha256,
                    diagnostic.exact_compiled_geometry_id,
                    diagnostic.exact_compiled_geometry_hash_sha256,
                    diagnostic.request.request_id,
                    diagnostic.model_dump_json(),
                    _utc_now(),
                ),
            )
            if retained is None:
                self._persist_diagnostic_inputs(connection, diagnostic, portal)
        return diagnostic

    def get_leak_portal_diagnostic(
        self,
        diagnostic_result_id: str,
    ) -> R120LeakPortalDiagnostic | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT diagnostic_result_id, diagnostic_hash_sha256, payload_json
                FROM cad_r120_leak_portal_diagnostics
                WHERE diagnostic_result_id=?
                """,
                (diagnostic_result_id,),
            ).fetchone()
        if row is None:
            return None
        diagnostic = R120LeakPortalDiagnostic.model_validate_json(
            row['payload_json']
        )
        if (
            diagnostic.diagnostic_result_id != row['diagnostic_result_id']
            or diagnostic.diagnostic_hash_sha256 != row['diagnostic_hash_sha256']
        ):
            raise ValueError(
                'persisted leak/portal diagnostic identity columns do not match payload'
            )
        return self._validated_diagnostic(diagnostic)

    def _validate_scene_binding(
        self,
        compiled: R120CompiledGeometry,
    ) -> SceneRevision:
        revision = self.scene_repository.get(
            compiled.exact_scene_revision_id
        )
        if revision is None:
            raise ValueError(
                'compiled geometry references an unpersisted SceneRevision'
            )
        if revision.content_hash != compiled.exact_scene_revision_content_hash:
            raise ValueError('compiled geometry SceneRevision content hash mismatch')
        geometry = revision.document.r120_semantic_geometry
        if geometry is None:
            raise ValueError(
                'compiled geometry SceneRevision no longer contains semantic geometry'
            )
        if geometry.geometry_id != compiled.exact_semantic_geometry_id:
            raise ValueError('compiled geometry SemanticAcousticGeometry id mismatch')
        if (
            geometry.semantic_hash_sha256
            != compiled.exact_semantic_geometry_hash_sha256
        ):
            raise ValueError(
                'compiled geometry SemanticAcousticGeometry hash mismatch'
            )
        return revision

    def _validated_compiled_row(
        self,
        row: sqlite3.Row,
    ) -> R120CompiledGeometry:
        compiled = R120CompiledGeometry.model_validate_json(row['payload_json'])
        if (
            compiled.compiled_geometry_id != row['compiled_geometry_id']
            or compiled.compiled_hash_sha256 != row['compiled_hash_sha256']
        ):
            raise ValueError(
                'persisted compiled geometry identity columns do not match payload'
            )
        return self._validated_compiled(compiled)

    def _validated_compiled(
        self,
        compiled: R120CompiledGeometry,
    ) -> R120CompiledGeometry:
        revision = self._validate_scene_binding(compiled)
        retained = self._retained_compile_inputs(compiled)
        if retained is None:
            raise ValueError(
                'compiled geometry has no retained compile input authorities'
            )
        inputs = self._resolve_compile_inputs(
            compiled,
            surface_boundary_bindings=None,
            region_authority=None,
            portal_authority=None,
            boundary_termination_authority=None,
            retained=retained,
        )
        self._require_canonical_recompile(compiled, revision, inputs)
        return compiled

    def _validated_diagnostic(
        self,
        diagnostic: R120LeakPortalDiagnostic,
    ) -> R120LeakPortalDiagnostic:
        compiled = self.get_compiled_geometry(
            diagnostic.exact_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError(
                'persisted leak/portal diagnostic references missing compiled geometry'
            )
        if (
            compiled.compiled_hash_sha256
            != diagnostic.exact_compiled_geometry_hash_sha256
        ):
            raise ValueError(
                'persisted leak/portal diagnostic compiled hash mismatch'
            )
        retained = self._retained_diagnostic_inputs(diagnostic)
        if retained is None:
            raise ValueError(
                'persisted leak/portal diagnostic has no retained input authorities'
            )
        portal = self._resolve_input_authority(
            ref=diagnostic.portal_authority_ref,
            supplied=None,
            retained=retained.portal_authority,
            expected_type=PortalAuthority,
            artifact='persisted leak/portal diagnostic',
            label='portal',
        )
        self._require_canonical_rediagnose(diagnostic, compiled, portal)
        return diagnostic

    def _require_canonical_recompile(
        self,
        compiled: R120CompiledGeometry,
        revision: SceneRevision,
        inputs: _CompileInputAuthorities,
    ) -> None:
        try:
            recompiled = compile_r120_geometry(
                revision,
                compiled.request,
                surface_boundary_bindings=inputs.surface_boundary_bindings,
                region_authority=inputs.region_authority,
                portal_authority=inputs.portal_authority,
                boundary_termination_authority=inputs.boundary_termination_authority,
            )
        except R120GeometryCompilationError as exc:
            raise ValueError(
                'compiled geometry retained inputs no longer satisfy the '
                'canonical R120 compiler'
            ) from exc
        if recompiled != compiled:
            raise ValueError(
                'compiled geometry does not reproduce from exact input authorities'
            )

    def _require_canonical_rediagnose(
        self,
        diagnostic: R120LeakPortalDiagnostic,
        compiled: R120CompiledGeometry,
        portal_authority: PortalAuthority | None,
    ) -> None:
        try:
            rediagnosed = diagnose_r120_leak_and_portals(
                compiled,
                diagnostic.request,
                portal_authority=portal_authority,
            )
        except R120GeometryCompilationError as exc:
            raise ValueError(
                'leak/portal diagnostic retained inputs no longer satisfy the '
                'canonical R120 diagnostic'
            ) from exc
        if rediagnosed != diagnostic:
            raise ValueError(
                'leak/portal diagnostic does not reproduce from exact input authorities'
            )

    def _resolve_compile_inputs(
        self,
        compiled: R120CompiledGeometry,
        *,
        surface_boundary_bindings: tuple[
            SurfaceBoundaryAuthorityBinding, ...
        ] | None,
        region_authority: AcousticRegionAuthority | None,
        portal_authority: PortalAuthority | None,
        boundary_termination_authority: BoundaryTerminationAuthority | None,
        retained: _CompileInputAuthorities | None,
    ) -> _CompileInputAuthorities:
        bindings = surface_boundary_bindings
        if bindings is None and retained is not None:
            bindings = retained.surface_boundary_bindings
        if bindings is None:
            bindings = self._derive_surface_bindings(compiled)
        return _CompileInputAuthorities(
            surface_boundary_bindings=tuple(bindings),
            region_authority=self._resolve_input_authority(
                ref=compiled.region_authority_ref,
                supplied=region_authority,
                retained=None if retained is None else retained.region_authority,
                expected_type=AcousticRegionAuthority,
                artifact='compiled geometry',
                label='acoustic region',
            ),
            portal_authority=self._resolve_input_authority(
                ref=compiled.portal_authority_ref,
                supplied=portal_authority,
                retained=None if retained is None else retained.portal_authority,
                expected_type=PortalAuthority,
                artifact='compiled geometry',
                label='portal',
            ),
            boundary_termination_authority=self._resolve_input_authority(
                ref=compiled.boundary_termination_authority_ref,
                supplied=boundary_termination_authority,
                retained=(
                    None
                    if retained is None
                    else retained.boundary_termination_authority
                ),
                expected_type=BoundaryTerminationAuthority,
                artifact='compiled geometry',
                label='boundary termination',
            ),
        )

    @staticmethod
    def _resolve_input_authority(
        *,
        ref: ExactExternalAuthorityRef | None,
        supplied: object,
        retained: object,
        expected_type: type,
        artifact: str,
        label: str,
    ):
        authority = supplied if supplied is not None else retained
        if ref is None:
            if authority is not None:
                raise ValueError(
                    f'{artifact} {label} authority supplied without a compiled ref'
                )
            return None
        if authority is None:
            raise ValueError(
                f'{artifact} references unretained {label} authority'
            )
        if not isinstance(authority, expected_type):
            raise ValueError(
                f'{artifact} {label} authority has unexpected type'
            )
        if _authority_ref_of(authority) != ref:
            raise ValueError(f'{artifact} {label} authority mismatch')
        return authority

    @staticmethod
    def _derive_surface_bindings(
        compiled: R120CompiledGeometry,
    ) -> tuple[SurfaceBoundaryAuthorityBinding, ...]:
        # The canonical compiler copies the material/boundary-physics refs it
        # consumes verbatim into the surface mapping, so the effective binding
        # set is recoverable from the persisted payload. Bindings that carried
        # only null refs are compile-time no-ops and need no retention.
        bindings: list[SurfaceBoundaryAuthorityBinding] = []
        for mapping in compiled.surface_mapping:
            if (
                mapping.material_authority is None
                and mapping.boundary_physics_authority is None
            ):
                continue
            bindings.append(
                SurfaceBoundaryAuthorityBinding(
                    source_surface_id=mapping.source_surface_id,
                    material_authority=mapping.material_authority,
                    boundary_physics_authority=mapping.boundary_physics_authority,
                )
            )
        return tuple(bindings)

    def _retained_compile_inputs(
        self,
        compiled: R120CompiledGeometry,
    ) -> _CompileInputAuthorities | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT compiled_hash_sha256, inputs_json
                FROM cad_r120_compile_inputs
                WHERE compiled_geometry_id=?
                """,
                (compiled.compiled_geometry_id,),
            ).fetchone()
        if row is None:
            return None
        if row['compiled_hash_sha256'] != compiled.compiled_hash_sha256:
            raise ValueError(
                'compiled geometry retained input authorities hash mismatch'
            )
        return _compile_inputs_from_json(row['inputs_json'])

    def _persist_compile_inputs(
        self,
        connection: sqlite3.Connection,
        compiled: R120CompiledGeometry,
        inputs: _CompileInputAuthorities,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cad_r120_compile_inputs(
                compiled_geometry_id,
                compiled_hash_sha256,
                inputs_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?)
            """,
            (
                compiled.compiled_geometry_id,
                compiled.compiled_hash_sha256,
                _compile_inputs_to_json(inputs),
                _utc_now(),
            ),
        )

    def _retained_diagnostic_inputs(
        self,
        diagnostic: R120LeakPortalDiagnostic,
    ) -> _DiagnosticInputAuthorities | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT diagnostic_hash_sha256, inputs_json
                FROM cad_r120_leak_diagnostic_inputs
                WHERE diagnostic_result_id=?
                """,
                (diagnostic.diagnostic_result_id,),
            ).fetchone()
        if row is None:
            return None
        if row['diagnostic_hash_sha256'] != diagnostic.diagnostic_hash_sha256:
            raise ValueError(
                'leak/portal diagnostic retained input authorities hash mismatch'
            )
        return _diagnostic_inputs_from_json(row['inputs_json'])

    def _persist_diagnostic_inputs(
        self,
        connection: sqlite3.Connection,
        diagnostic: R120LeakPortalDiagnostic,
        portal_authority: PortalAuthority | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cad_r120_leak_diagnostic_inputs(
                diagnostic_result_id,
                diagnostic_hash_sha256,
                inputs_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?)
            """,
            (
                diagnostic.diagnostic_result_id,
                diagnostic.diagnostic_hash_sha256,
                _diagnostic_inputs_to_json(portal_authority),
                _utc_now(),
            ),
        )
