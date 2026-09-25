from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from .cad_adaptive_planner import production_validation_ready
from .cad_extended_search import (
    CadExtendedModelCapability,
    CadExtendedParameterEvidence,
    CadExtendedParameterEvidenceRef,
    CadExtendedSearchSpec,
    generate_extended_candidates,
)
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_search import generate_cad_candidates
from .cad_search_repository import CadSearchRepository
from .cad_schema import require_native_tables

if TYPE_CHECKING:
    # CadRobustnessRepository already depends on this module, so the
    # optional O90E resolver dependency must not create an import cycle.
    from .cad_robustness_validation_repository import (
        CadRobustnessValidationRepository,
    )


class CadExtendedSearchRepository:
    """Immutable O80 capability/spec storage layered on base O10 SearchSpec authority."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        validation_repository: CadModelValidationRepository | None = None,
        robustness_validation_repository: (
            CadRobustnessValidationRepository | None
        ) = None,
    ) -> None:
        self.search_repository = search_repository
        self.validation_repository = validation_repository
        self.robustness_validation_repository = robustness_validation_repository
        self.path = Path(search_repository.path)
        if (
            validation_repository is not None
            and Path(validation_repository.path) != self.path
        ):
            raise ValueError(
                'extended search validation repository must share native CAD database'
            )
        if (
            robustness_validation_repository is not None
            and Path(robustness_validation_repository.path) != self.path
        ):
            raise ValueError(
                'extended search robustness repository must share native CAD database'
            )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_extended_model_capabilities', 'cad_extended_parameter_evidence', 'cad_extended_search_specs')

    def _resolve_evidence_source(
        self,
        evidence: CadExtendedParameterEvidence,
    ) -> None:
        """Re-resolve the exact authority one parameter evidence binds to (#384).

        ``o90e_decision`` sources must resolve through the O90E repository's
        own replay path to an eligible decision for the same model/version
        whose axis coverage includes the declared parameter over the full
        declared tested range. ``synthetic_fixture`` sources are
        declared-only development claims and carry no persisted authority.
        """

        if evidence.source_kind == 'synthetic_fixture':
            if evidence.evidence_scope != 'synthetic_fixture':
                raise ValueError(
                    'owned-room parameter evidence cannot use a synthetic '
                    'source'
                )
            return

        repository = self.robustness_validation_repository
        if repository is None:
            raise ValueError(
                'o90e parameter evidence requires a robustness validation '
                'repository'
            )
        decision = repository.get_decision(evidence.source_id)
        if decision is None:
            raise ValueError(
                'extended parameter evidence source does not resolve to a '
                f'persisted O90E decision: {evidence.source_id}'
            )
        if decision.decision_sha256 != evidence.source_sha256:
            raise ValueError(
                'extended parameter evidence source hash mismatch: '
                f'{evidence.source_id}'
            )
        if decision.production_gate != 'eligible':
            raise ValueError(
                'extended parameter evidence requires an eligible O90E '
                'decision'
            )
        if (
            decision.model_id != evidence.model_id
            or decision.model_version != evidence.model_version
        ):
            raise ValueError(
                'extended parameter evidence model does not match O90E '
                'decision'
            )
        try:
            spec = repository.robustness_repository.get_spec(
                decision.robustness_spec_id
            )
        except KeyError as exc:
            raise ValueError(
                'extended parameter evidence O90E robustness spec is missing'
            ) from exc
        if spec.robustness_spec_sha256 != decision.robustness_spec_sha256:
            raise ValueError(
                'extended parameter evidence O90E spec hash mismatch'
            )
        axis_by_id = {axis.axis_id: axis for axis in spec.axes}
        for coverage in decision.axis_coverage:
            if coverage.state != 'full':
                continue
            axis = axis_by_id.get(coverage.axis_id)
            if (
                axis is None
                or axis.parameter != evidence.parameter
                or axis.unit != 'deg'
                or coverage.tested_minus_delta is None
                or coverage.tested_plus_delta is None
            ):
                continue
            tested_low = axis.nominal_value - coverage.tested_minus_delta
            tested_high = axis.nominal_value + coverage.tested_plus_delta
            if (
                tested_low <= evidence.tested_min_deg
                and tested_high >= evidence.tested_max_deg
            ):
                return
        raise ValueError(
            'extended parameter evidence claims a range no eligible O90E '
            f'axis coverage proves: {evidence.parameter} '
            f'[{evidence.tested_min_deg}, {evidence.tested_max_deg}]'
        )

    def _read_parameter_evidence_row(
        self,
        row: sqlite3.Row,
    ) -> CadExtendedParameterEvidence:
        evidence = CadExtendedParameterEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            row['evidence_id'] != evidence.evidence_id
            or row['parameter'] != evidence.parameter
            or row['model_id'] != evidence.model_id
            or row['model_version'] != evidence.model_version
            or row['evidence_scope'] != evidence.evidence_scope
            or row['evidence_sha256'] != evidence.evidence_sha256
            or row['created_at_utc'] != evidence.created_at_utc
        ):
            raise ValueError(
                'persisted extended parameter evidence row disagrees with '
                'its payload'
            )
        self._resolve_evidence_source(evidence)
        return evidence

    def save_parameter_evidence(
        self,
        evidence: CadExtendedParameterEvidence,
    ) -> None:
        """Persist one parameter evidence record after source replay."""

        evidence = CadExtendedParameterEvidence.model_validate(
            evidence.model_dump(mode='python')
        )
        self._resolve_evidence_source(evidence)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_extended_parameter_evidence '
                'WHERE evidence_id=? OR evidence_sha256=?',
                (evidence.evidence_id, evidence.evidence_sha256),
            ).fetchone() is not None:
                raise ValueError(
                    'extended parameter evidence already exists: '
                    f'{evidence.evidence_id}'
                )
            connection.execute(
                """
                INSERT INTO cad_extended_parameter_evidence(
                    evidence_id, parameter, model_id, model_version,
                    evidence_scope, evidence_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.parameter,
                    evidence.model_id,
                    evidence.model_version,
                    evidence.evidence_scope,
                    evidence.evidence_sha256,
                    evidence.model_dump_json(),
                    evidence.created_at_utc,
                ),
            )

    def get_parameter_evidence(
        self,
        evidence_id: str,
    ) -> CadExtendedParameterEvidence | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_extended_parameter_evidence '
                'WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._read_parameter_evidence_row(row)
        )

    def list_parameter_evidence(
        self,
    ) -> tuple[CadExtendedParameterEvidence, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_extended_parameter_evidence '
                'ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            self._read_parameter_evidence_row(row) for row in rows
        )

    def _require_capability_authority(
        self,
        capability: CadExtendedModelCapability,
    ) -> None:
        """Replay the exact authority one capability claims (#384).

        Every supported parameter must resolve to a persisted evidence
        record with its exact semantic hash, the same parameter, the same
        model id/version, and the same evidence scope — a generic O60
        model-eligibility record alone can never authorize a directional
        parameter. Owned-room capabilities additionally keep the
        model-level O60 eligibility gate.
        """

        for ref in capability.parameter_evidence:
            evidence = self.get_parameter_evidence(ref.evidence_id)
            if evidence is None:
                raise ValueError(
                    'extended capability parameter evidence does not '
                    f'resolve: {ref.evidence_id}'
                )
            if evidence.evidence_sha256 != ref.evidence_sha256:
                raise ValueError(
                    'extended capability parameter evidence hash mismatch: '
                    f'{ref.evidence_id}'
                )
            if evidence.parameter != ref.parameter:
                raise ValueError(
                    'extended capability parameter evidence parameter '
                    'mismatch'
                )
            if (
                evidence.model_id != capability.model_id
                or evidence.model_version != capability.model_version
            ):
                raise ValueError(
                    'extended capability parameter evidence model mismatch'
                )
            if evidence.evidence_scope != capability.evidence_scope:
                raise ValueError(
                    'extended capability parameter evidence scope mismatch'
                )
        if capability.evidence_scope == 'owned_room':
            repository = self.validation_repository
            if repository is None:
                raise ValueError(
                    'owned-room extended capability requires validation repository'
                )
            if capability.validation_id is None:
                raise ValueError('owned-room extended capability has no ValidationRecord')
            validation = repository.get(capability.validation_id)
            if validation is None or not production_validation_ready(validation):
                raise ValueError(
                    'owned-room extended capability ValidationRecord is not eligible'
                )
            if (
                validation.model_id != capability.model_id
                or validation.model_version != capability.model_version
            ):
                raise ValueError(
                    'extended capability model does not match ValidationRecord'
                )

    def save_capability(self, capability: CadExtendedModelCapability) -> None:
        capability = CadExtendedModelCapability.model_validate(
            capability.model_dump(mode='python')
        )
        self._require_capability_authority(capability)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                """
                INSERT INTO cad_extended_model_capabilities(
                    capability_id, model_id, model_version, evidence_scope,
                    capability_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.model_id,
                    capability.model_version,
                    capability.evidence_scope,
                    capability.capability_sha256,
                    capability.model_dump_json(),
                    capability.created_at_utc,
                ),
            )

    def _read_capability_row(
        self,
        row: sqlite3.Row,
    ) -> CadExtendedModelCapability:
        capability = CadExtendedModelCapability.model_validate_json(
            row['payload_json']
        )
        if (
            row['capability_id'] != capability.capability_id
            or row['model_id'] != capability.model_id
            or row['model_version'] != capability.model_version
            or row['evidence_scope'] != capability.evidence_scope
            or row['capability_sha256'] != capability.capability_sha256
            or row['created_at_utc'] != capability.created_at_utc
        ):
            raise ValueError(
                'persisted extended capability row disagrees with its payload'
            )
        self._require_capability_authority(capability)
        return capability

    def get_capability(
        self,
        capability_id: str,
    ) -> CadExtendedModelCapability | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_extended_model_capabilities '
                'WHERE capability_id=?',
                (capability_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._read_capability_row(row)
        )

    def list_capabilities(self) -> tuple[CadExtendedModelCapability, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_extended_model_capabilities '
                'ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            self._read_capability_row(row) for row in rows
        )

    def _base_page(self, spec) -> object:
        return generate_cad_candidates(
            self.search_repository.scene_repository,
            spec,
            offset=0,
            limit=1,
        )

    def _require_spec_authority(self, spec: CadExtendedSearchSpec) -> None:
        """Replay the exact authority one extended SearchSpec binds to (#384).

        Revalidates the base SearchSpec/source-revision/candidate-set
        binding and the capability — including its per-parameter evidence —
        then requires every axis to stay inside the tested applicability
        range its backing evidence proves. Used by ``save_spec`` and by
        every authoritative read so a stale, forged, or out-of-range spec
        fails closed instead of silently feeding O80/O90/O100 consumers.
        """

        base = self.search_repository.get(spec.base_search_spec_id)
        if base is None:
            raise ValueError('extended search base SearchSpec does not exist')
        if (
            base.document_id != spec.document_id
            or base.search_spec_sha256 != spec.base_search_spec_sha256
        ):
            raise ValueError('extended search base SearchSpec authority mismatch')
        source = self.search_repository.scene_repository.get(base.scene_revision_id)
        if (
            source is None
            or source.document_id != spec.document_id
            or source.content_hash != base.scene_content_hash
        ):
            raise ValueError('extended search source revision authority mismatch')

        capability = self.get_capability(spec.capability_id)
        if capability is None:
            raise ValueError('extended search model capability does not exist')
        if capability.capability_sha256 != spec.capability_sha256:
            raise ValueError('extended search model capability hash mismatch')
        if capability.evidence_scope == 'owned_room':
            repository = self.validation_repository
            if repository is None or capability.validation_id is None:
                raise ValueError(
                    'owned-room extended search requires validation repository authority'
                )
            validation = repository.get(capability.validation_id)
            if validation is None or not production_validation_ready(validation):
                raise ValueError(
                    'owned-room extended search ValidationRecord is not eligible'
                )
            if (
                validation.document_id != spec.document_id
                or validation.search_spec_id != spec.base_search_spec_id
                or validation.search_spec_sha256 != spec.base_search_spec_sha256
                or validation.candidate_set_sha256 != spec.base_candidate_set_sha256
                or validation.model_id != capability.model_id
                or validation.model_version != capability.model_version
            ):
                raise ValueError(
                    'owned-room extended capability does not match exact base '
                    'SearchSpec/candidate-set validation authority'
                )
        for axis in spec.axes:
            if axis.parameter not in capability.supported_parameters:
                raise ValueError(
                    f'extended model capability does not support {axis.parameter}'
                )
            # The capability may only promise a parameter inside the range
            # its bound evidence actually tested (#384).
            evidence = self.get_parameter_evidence(
                capability.evidence_for(axis.parameter).evidence_id
            )
            if evidence is None:
                raise ValueError(
                    'extended axis parameter evidence does not resolve'
                )
            if (
                axis.min_value < evidence.tested_min_deg
                or axis.max_value > evidence.tested_max_deg
            ):
                raise ValueError(
                    f'extended axis {axis.parameter} range '
                    f'[{axis.min_value}, {axis.max_value}] exceeds the '
                    'tested applicability '
                    f'[{evidence.tested_min_deg}, {evidence.tested_max_deg}]'
                )
            try:
                entity = source.document.entity(axis.entity_id)
            except KeyError as exc:
                raise ValueError(
                    f'extended search axis references unknown entity: {axis.entity_id}'
                ) from exc
            if entity.kind != 'speaker':
                raise ValueError(
                    f'extended aim axis requires a speaker: {axis.entity_id}'
                )
            if entity.aim_xyz is None:
                raise ValueError(
                    f'extended aim axis requires explicit speaker aim: {axis.entity_id}'
                )
            horizontal = (
                float(entity.aim_xyz.x) ** 2 + float(entity.aim_xyz.y) ** 2
            ) ** 0.5
            if horizontal <= 1e-9:
                raise ValueError(
                    f'extended aim axis cannot rotate vertical-only aim: {axis.entity_id}'
                )

        page = self._base_page(base)
        if page.candidate_set_sha256 != spec.base_candidate_set_sha256:
            raise ValueError('extended search base candidate-set hash mismatch')

        # Full generation is the repository-level replay of axis/entity/capability
        # semantics and immutable candidate-limit authority.
        generate_extended_candidates(
            self.search_repository.scene_repository,
            base,
            spec,
            offset=0,
            limit=1,
        )

    def save_spec(self, spec: CadExtendedSearchSpec) -> None:
        spec = CadExtendedSearchSpec.model_validate(spec.model_dump(mode='python'))
        self._require_spec_authority(spec)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                """
                INSERT INTO cad_extended_search_specs(
                    extended_search_id, document_id, base_search_spec_id,
                    capability_id, extended_search_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.extended_search_id,
                    spec.document_id,
                    spec.base_search_spec_id,
                    spec.capability_id,
                    spec.extended_search_sha256,
                    spec.model_dump_json(),
                    spec.created_at_utc,
                ),
            )

    def _read_spec_row(self, row: sqlite3.Row) -> CadExtendedSearchSpec:
        spec = CadExtendedSearchSpec.model_validate_json(row['payload_json'])
        if (
            row['extended_search_id'] != spec.extended_search_id
            or row['document_id'] != spec.document_id
            or row['base_search_spec_id'] != spec.base_search_spec_id
            or row['capability_id'] != spec.capability_id
            or row['extended_search_sha256'] != spec.extended_search_sha256
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted extended search spec row disagrees with its payload'
            )
        self._require_spec_authority(spec)
        return spec

    def get_spec(self, extended_search_id: str) -> CadExtendedSearchSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_extended_search_specs '
                'WHERE extended_search_id=?',
                (extended_search_id,),
            ).fetchone()
        return None if row is None else self._read_spec_row(row)

    def list_for_base_search(
        self,
        base_search_spec_id: str,
    ) -> tuple[CadExtendedSearchSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_extended_search_specs '
                'WHERE base_search_spec_id=? ORDER BY seq ASC',
                (base_search_spec_id,),
            ).fetchall()
        return tuple(self._read_spec_row(row) for row in rows)
