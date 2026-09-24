"""Repository-backed authority boundary for installation reports (issue #420).

``htdt.report.build_installation_output`` is a deterministic pure compiler: it
checks caller-supplied objects against each other but cannot prove they came
from the authoritative repositories. ``InstallationReportService`` is the
production report boundary — every optional report section is resolved through
its authoritative repository's canonical read/replay validator, generic
evidence references resolve only against persisted authority applicable to the
exact Scene/SystemVariant target, and the pure compiler only ever sees
validated canonical objects:

- ``VideoGeometryEvaluation``/``ProjectorSpecification`` load through
  ``CadVideoGeometryRepository``'s canonical replay (issue #405): a
  self-hash-valid but non-canonical evaluation cannot enter the report.
- ``StandardsEvaluation`` is resolved from ``CadStandardsRepository`` and
  replayed through ``evaluate_standards_profile`` against the persisted
  profile and stored observations; published criterion sources must still
  resolve to retained source authority. Fabricated result rows or evidence
  claims fail closed.
- ``AcousticTreatment`` placements come from
  ``CadAcousticTreatmentRepository`` reads (definition evidence and semantic
  host binding are revalidated on decode), and every surface binding is
  re-evaluated by the repository against the exact bound SceneRevision —
  caller-supplied evaluations are never consulted.
- ``CadCalibrationPlan``/exports/verification contracts/lifecycle events load
  through ``CadCalibrationRepository``'s canonical reads (source-authority and
  support-state replay, canonical export rebuild, registration attestation and
  the single-head lifecycle chain — issues #354/#358).

``verify_installation_output_replay`` rebuilds a persisted/exported
``InstallationOutput`` from the exact authority ids/hashes it records and
requires semantic equality, so missing or tampered source authority fails
closed rather than preserving a misleading ``AVAILABLE`` section.
"""

from __future__ import annotations

from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from .cad_acoustic_treatment import (
    AcousticTreatmentDefinition,
    AcousticTreatmentPlacement,
    TreatmentSurfaceBindingEvaluation,
)
from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_calibration import (
    CadCalibrationExportSnapshot,
    CadCalibrationLifecycleEvent,
    CadCalibrationPlan,
    CadVerificationMeasurementPlan,
)
from .cad_calibration_repository import CadCalibrationRepository
from .cad_cable_run import CableRun
from .cad_cable_run_repository import CadCableRunRepository
from .cad_installation_datum import InstallationDatum
from .cad_installation_datum_repository import CadInstallationDatumRepository
from .cad_measurement_quality import dataset_sha256, measurement_sha256
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_standards import (
    StandardsEvaluation,
    StandardsProfile,
    evaluate_standards_profile,
    validate_criterion_source_authority,
)
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_video_geometry import (
    ProjectorSpecification,
    VideoGeometryEvaluation,
)
from .cad_video_geometry_repository import CadVideoGeometryRepository
from .report import (
    INSTALLATION_OUTPUT_AUTHORITY_VERSION,
    INSTALLATION_OUTPUT_SCHEMA_VERSION,
    InstallationEvidenceRef,
    InstallationOutput,
    build_installation_output,
)


class InstallationAuthorityError(ValueError):
    """An installation report authority read or resolve failed closed."""


class InstallationTreatmentInstanceRef(BaseModel):
    """Exact pin of one persisted treatment placement version for a report."""

    model_config = ConfigDict(frozen=True)

    instance_id: str = Field(min_length=1)
    placement_version: int = Field(ge=1)
    placement_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class InstallationCableRunRef(BaseModel):
    """Exact pin of one persisted cable-run version for a report."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    run_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


# Installation evidence namespaces and the store each resolves against. The
# ``authority`` field of an InstallationEvidenceRef is a type discriminator —
# it names which authoritative store must produce the referenced evidence —
# never a free-form provenance label. ``evidence_id`` is the store's primary
# key; ``evidence_sha256`` is the exact pinned semantic hash (verified whenever
# the store exposes one); ``evidence_type`` carries the authority version where
# the store key is (id, version). Any ref that does not resolve, does not apply
# to the installation target, or names an unsupported authority is rejected.
SUPPORTED_INSTALLATION_EVIDENCE_AUTHORITIES: tuple[str, ...] = (
    'scene_revision',
    'scene_content_blob',
    'system_variant',
    'system_variant.proposal_evidence',
    'projector_specification',
    'video_geometry_evaluation',
    'standards_profile',
    'standards_evaluation',
    'standards_source_authority',
    'treatment_evidence',
    'treatment_definition',
    'treatment_placement',
    'calibration_plan',
    'calibration_export',
    'calibration_verification_plan',
    'measurement',
    'measurement_dataset',
    'measurement_quality_report',
    'installation_datum',
    'cable_run',
)

# Builder-derived refs: proposal evidence is re-read from the resolved
# SystemVariant on every build, so replay verification must not pass the
# recorded copies back as caller-supplied evidence.
_DERIVED_EVIDENCE_AUTHORITIES = frozenset({'system_variant.proposal_evidence'})


class InstallationReportService:
    """Production installation-report boundary over the authoritative stores.

    Every repository must share the same native CAD database so that the
    Scene/SystemVariant authority resolved at the top is the same authority
    each section store replays against.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository | None = None,
        video_geometry_repository: CadVideoGeometryRepository | None = None,
        standards_repository: CadStandardsRepository | None = None,
        treatment_repository: CadAcousticTreatmentRepository | None = None,
        calibration_repository: CadCalibrationRepository | None = None,
        measurement_repository: CadMeasurementRepository | None = None,
        measurement_quality_repository: CadMeasurementQualityRepository | None = None,
        datum_repository: CadInstallationDatumRepository | None = None,
        cable_run_repository: CadCableRunRepository | None = None,
    ) -> None:
        repositories = (
            scene_repository,
            system_variant_repository,
            video_geometry_repository,
            standards_repository,
            treatment_repository,
            calibration_repository,
            measurement_repository,
            measurement_quality_repository,
            datum_repository,
            cable_run_repository,
        )
        paths = {str(repository.path) for repository in repositories if repository is not None}
        if len(paths) != 1:
            raise InstallationAuthorityError(
                'installation report repositories must share one native CAD database'
            )
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.video_geometry_repository = video_geometry_repository
        self.standards_repository = standards_repository
        self.treatment_repository = treatment_repository
        self.calibration_repository = calibration_repository
        self.measurement_repository = measurement_repository
        self.measurement_quality_repository = measurement_quality_repository
        self.datum_repository = datum_repository
        self.cable_run_repository = cable_run_repository

    # -- repository fallbacks ------------------------------------------------

    def _variant_repository(self) -> CadSystemVariantRepository | None:
        if self.system_variant_repository is not None:
            return self.system_variant_repository
        for repository in (
            self.video_geometry_repository,
            self.standards_repository,
            self.treatment_repository,
            self.calibration_repository,
        ):
            if repository is None:
                continue
            candidate = getattr(repository, 'variant_repository', None) or getattr(
                repository, 'system_variant_repository', None
            )
            if candidate is not None:
                return candidate
        return None

    def _measurements(self) -> CadMeasurementRepository | None:
        if self.measurement_repository is not None:
            return self.measurement_repository
        if self.calibration_repository is not None:
            return self.calibration_repository.measurement_repository
        return None

    def _quality(self) -> CadMeasurementQualityRepository | None:
        if self.measurement_quality_repository is not None:
            return self.measurement_quality_repository
        if self.calibration_repository is not None:
            return self.calibration_repository.quality_repository
        return None

    # -- shared binding checks ----------------------------------------------

    @staticmethod
    def _require_scene_binding(
        *,
        revision: SceneRevision,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        label: str,
    ) -> None:
        if (
            document_id != revision.document_id
            or scene_revision_id != revision.revision_id
            or scene_content_hash != revision.content_hash
        ):
            raise InstallationAuthorityError(
                f'{label} is not bound to the exact installation SceneRevision'
            )

    @classmethod
    def _require_target_binding(
        cls,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        system_variant_id: str | None,
        system_variant_sha256: str | None,
        label: str,
    ) -> None:
        """Require one authority target to equal the exact installation target."""

        cls._require_scene_binding(
            revision=revision,
            document_id=document_id,
            scene_revision_id=scene_revision_id,
            scene_content_hash=scene_content_hash,
            label=label,
        )
        expected_id = None if variant is None else variant.variant_id
        expected_sha256 = None if variant is None else variant.variant_sha256
        if (
            system_variant_id != expected_id
            or system_variant_sha256 != expected_sha256
        ):
            raise InstallationAuthorityError(
                f'{label} is not bound to the exact installation SystemVariant'
            )

    @staticmethod
    def _require_ref_hash(
        ref: InstallationEvidenceRef,
        actual_sha256: str | None,
        label: str,
    ) -> None:
        if ref.evidence_sha256 is not None and ref.evidence_sha256 != actual_sha256:
            raise InstallationAuthorityError(
                f'installation evidence {label} semantic hash mismatch: '
                f'{ref.authority}/{ref.evidence_id}'
            )

    # -- top-level authority resolution --------------------------------------

    def _resolve_revision(self, scene_revision_id: str) -> SceneRevision:
        revision = self.scene_repository.get(scene_revision_id)
        if revision is None:
            raise InstallationAuthorityError(
                f'installation SceneRevision does not exist: {scene_revision_id}'
            )
        return revision

    def _resolve_variant(
        self,
        revision: SceneRevision,
        system_variant_id: str | None,
    ) -> SystemVariant | None:
        if system_variant_id is None:
            return None
        repository = self._variant_repository()
        if repository is None:
            raise InstallationAuthorityError(
                'installation SystemVariant resolution requires a '
                'CadSystemVariantRepository authority'
            )
        variant = repository.get_variant(system_variant_id)
        if variant is None:
            raise InstallationAuthorityError(
                f'installation SystemVariant does not exist: {system_variant_id}'
            )
        if (
            variant.document_id != revision.document_id
            or variant.baseline_revision_id != revision.revision_id
            or variant.baseline_content_hash != revision.content_hash
        ):
            raise InstallationAuthorityError(
                'installation SystemVariant baseline is not the resolved '
                'SceneRevision authority'
            )
        return variant

    # -- per-section canonical resolution -------------------------------------

    def _resolve_video_geometry(
        self,
        evaluation_id: str | None,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
    ) -> tuple[ProjectorSpecification | None, VideoGeometryEvaluation | None]:
        if evaluation_id is None:
            return None, None
        if self.video_geometry_repository is None:
            raise InstallationAuthorityError(
                'installation video geometry requires a '
                'CadVideoGeometryRepository authority'
            )
        evaluation = self.video_geometry_repository.get_evaluation(evaluation_id)
        if evaluation is None:
            raise InstallationAuthorityError(
                f'installation VideoGeometryEvaluation does not exist: {evaluation_id}'
            )
        if evaluation.evaluation_id != evaluation_id:
            raise InstallationAuthorityError(
                'persisted video geometry evaluation identity mismatch'
            )
        target = evaluation.target
        self._require_target_binding(
            revision=revision,
            variant=variant,
            document_id=target.document_id,
            scene_revision_id=target.scene_revision_id,
            scene_content_hash=target.scene_content_hash,
            system_variant_id=target.system_variant_id,
            system_variant_sha256=target.system_variant_sha256,
            label='installation VideoGeometryEvaluation',
        )
        specification = self.video_geometry_repository.get_projector_specification_by_hash(
            evaluation.projector_specification_sha256
        )
        if specification is None:
            raise InstallationAuthorityError(
                'installation ProjectorSpecification does not exist'
            )
        request = evaluation.request
        if (
            specification.specification_id != request.projector_specification_id
            or specification.version != request.projector_specification_version
            or specification.specification_sha256
            != request.projector_specification_sha256
        ):
            raise InstallationAuthorityError(
                'installation ProjectorSpecification does not match the '
                'evaluation request authority'
            )
        return specification, evaluation

    def _resolve_standards(
        self,
        evaluation_id: str | None,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
    ) -> tuple[StandardsProfile | None, StandardsEvaluation | None]:
        if evaluation_id is None:
            return None, None
        if self.standards_repository is None:
            raise InstallationAuthorityError(
                'installation standards evaluation requires a '
                'CadStandardsRepository authority'
            )
        evaluation = self.standards_repository.get_evaluation(evaluation_id)
        if evaluation is None:
            raise InstallationAuthorityError(
                f'installation StandardsEvaluation does not exist: {evaluation_id}'
            )
        if evaluation.evaluation_id != evaluation_id:
            raise InstallationAuthorityError(
                'persisted standards evaluation identity mismatch'
            )
        target = evaluation.target
        self._require_target_binding(
            revision=revision,
            variant=variant,
            document_id=target.document_id,
            scene_revision_id=target.scene_revision_id,
            scene_content_hash=target.scene_content_hash,
            system_variant_id=target.system_variant_id,
            system_variant_sha256=target.system_variant_sha256,
            label='installation StandardsEvaluation',
        )
        profile = self.standards_repository.get_profile(
            evaluation.profile_id,
            evaluation.profile_version,
        )
        if profile is None:
            raise InstallationAuthorityError(
                'installation StandardsProfile does not exist'
            )
        if profile.profile_semantic_hash != evaluation.profile_semantic_hash:
            raise InstallationAuthorityError(
                'installation StandardsEvaluation profile authority mismatch'
            )
        # Canonical replay: the persisted row must be exactly what the
        # standards evaluator derives from the stored profile, target and
        # observations — a fabricated result or evidence claim cannot enter.
        regenerated = evaluate_standards_profile(
            profile=profile,
            target=evaluation.target,
            observations=evaluation.observations,
            created_at_utc=evaluation.created_at_utc,
            reevaluation_of_id=evaluation.reevaluation_of_id,
        )
        if regenerated != evaluation:
            raise InstallationAuthorityError(
                'installation StandardsEvaluation does not reproduce from '
                'canonical evaluator authority'
            )
        for criterion in profile.criteria:
            ref = criterion.source.authority_ref
            if ref is None:
                continue
            resolved = self.standards_repository.resolve_source_authority(ref)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation standards criterion {criterion.criterion_id} '
                    'source authority does not resolve to retained evidence'
                )
            try:
                validate_criterion_source_authority(criterion, resolved)
            except ValueError as exc:
                raise InstallationAuthorityError(
                    f'installation standards criterion {criterion.criterion_id} '
                    f'source provenance rejected: {exc}'
                ) from exc
        return profile, evaluation

    def _resolve_treatment(
        self,
        treatment_instances: Sequence[InstallationTreatmentInstanceRef] | None,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
    ) -> tuple[
        tuple[AcousticTreatmentDefinition, ...],
        tuple[AcousticTreatmentPlacement, ...],
        tuple[TreatmentSurfaceBindingEvaluation, ...],
    ]:
        if self.treatment_repository is None:
            if treatment_instances:
                raise InstallationAuthorityError(
                    'installation treatment pins require a '
                    'CadAcousticTreatmentRepository authority'
                )
            return (), (), ()
        repository = self.treatment_repository

        placements: list[AcousticTreatmentPlacement] = []
        if treatment_instances is None:
            # Discover every current placement bound to the exact target:
            # baseline-scoped placements always apply; variant-scoped
            # placements apply only when they name the resolved variant.
            applicable: dict[str, AcousticTreatmentPlacement] = {}
            for placement in repository.list_placements_for_scene(revision.revision_id):
                if placement.system_variant_id is not None:
                    if variant is None or placement.system_variant_id != variant.variant_id:
                        continue
                    if placement.system_variant_sha256 != variant.variant_sha256:
                        raise InstallationAuthorityError(
                            'installation treatment SystemVariant authority mismatch'
                        )
                current = applicable.get(placement.instance_id)
                if current is None or placement.placement_version > current.placement_version:
                    applicable[placement.instance_id] = placement
            placements = sorted(
                applicable.values(),
                key=lambda item: (item.instance_id, item.placement_version),
            )
        else:
            for pin in sorted(
                treatment_instances,
                key=lambda item: (item.instance_id, item.placement_version),
            ):
                placement = repository.get_placement(
                    pin.instance_id,
                    pin.placement_version,
                )
                if placement is None:
                    raise InstallationAuthorityError(
                        'installation treatment placement does not exist: '
                        f'{pin.instance_id}@{pin.placement_version}'
                    )
                if (
                    pin.placement_sha256 is not None
                    and pin.placement_sha256 != placement.placement_sha256
                ):
                    raise InstallationAuthorityError(
                        'installation treatment placement semantic hash mismatch'
                    )
                self._require_scene_binding(
                    revision=revision,
                    document_id=placement.document_id,
                    scene_revision_id=placement.scene_revision_id,
                    scene_content_hash=placement.scene_content_hash,
                    label='installation treatment placement',
                )
                if placement.system_variant_id is not None and (
                    variant is None
                    or placement.system_variant_id != variant.variant_id
                    or placement.system_variant_sha256 != variant.variant_sha256
                ):
                    raise InstallationAuthorityError(
                        'installation treatment placement is not bound to the '
                        'installation SystemVariant'
                    )
                placements.append(placement)

        definitions: dict[tuple[str, str], AcousticTreatmentDefinition] = {}
        evaluations: list[TreatmentSurfaceBindingEvaluation] = []
        for placement in placements:
            definition = repository.get_definition(
                placement.definition_id,
                placement.definition_version,
            )
            if (
                definition is None
                or definition.definition_sha256 != placement.definition_sha256
            ):
                raise InstallationAuthorityError(
                    'installation treatment placement references an invalid '
                    'definition authority'
                )
            definitions[(definition.definition_id, definition.version)] = definition
            # The host binding is re-evaluated by the repository against the
            # exact bound SceneRevision on every report — never trusted from a
            # caller-supplied evaluation object.
            evaluations.append(
                repository.evaluate_placement_surface_binding(
                    placement,
                    scene_revision_id=revision.revision_id,
                )
            )
        return tuple(definitions.values()), tuple(placements), tuple(evaluations)

    def _resolve_calibration(
        self,
        plan_id: str | None,
        export_id: str | None,
        verification_plan_id: str | None,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
    ) -> tuple[
        CadCalibrationPlan | None,
        CadCalibrationExportSnapshot | None,
        CadVerificationMeasurementPlan | None,
        tuple[CadCalibrationLifecycleEvent, ...],
    ]:
        if plan_id is None:
            if export_id is not None or verification_plan_id is not None:
                raise InstallationAuthorityError(
                    'installation calibration export/verification pins require '
                    'a CalibrationPlan authority'
                )
            return None, None, None, ()
        if self.calibration_repository is None:
            raise InstallationAuthorityError(
                'installation calibration requires a CadCalibrationRepository '
                'authority'
            )
        repository = self.calibration_repository
        plan = repository.get_plan(plan_id)
        if plan is None:
            raise InstallationAuthorityError(
                f'installation CalibrationPlan does not exist: {plan_id}'
            )
        if plan.plan_id != plan_id:
            raise InstallationAuthorityError(
                'persisted calibration plan identity mismatch'
            )
        self._require_target_binding(
            revision=revision,
            variant=variant,
            document_id=plan.document_id,
            scene_revision_id=plan.scene_revision_id,
            scene_content_hash=plan.scene_content_hash,
            system_variant_id=plan.system_variant_id,
            system_variant_sha256=plan.system_variant_sha256,
            label='installation CalibrationPlan',
        )
        export = None
        if export_id is not None:
            export = repository.get_export(export_id)
            if export is None:
                raise InstallationAuthorityError(
                    f'installation calibration export does not exist: {export_id}'
                )
            if (
                export.export_id != export_id
                or export.calibration_plan_id != plan.plan_id
                or export.requested_plan_semantic_sha256 != plan.plan_semantic_sha256
            ):
                raise InstallationAuthorityError(
                    'installation calibration export is not bound to the '
                    'resolved CalibrationPlan'
                )
        verification = None
        if verification_plan_id is not None:
            verification = repository.get_verification_plan(verification_plan_id)
            if verification is None:
                raise InstallationAuthorityError(
                    'installation verification measurement plan does not exist: '
                    f'{verification_plan_id}'
                )
            if verification.verification_plan_id != verification_plan_id:
                raise InstallationAuthorityError(
                    'persisted verification measurement plan identity mismatch'
                )
            if export is None:
                raise InstallationAuthorityError(
                    'installation verification measurement plan requires the '
                    'exact persisted export snapshot'
                )
            if (
                verification.calibration_plan_id != plan.plan_id
                or verification.calibration_plan_semantic_sha256
                != plan.plan_semantic_sha256
                or verification.exported_settings_id != export.export_id
                or verification.exported_settings_semantic_sha256
                != export.exported_settings_semantic_sha256
            ):
                raise InstallationAuthorityError(
                    'installation verification measurement plan is not bound to '
                    'the resolved plan/export authority'
                )
            # Issue #358: the contract is only preregistered evidence when its
            # durable registration exists — a legacy post-hoc row is readable
            # for audit but is not an attested contract for a report.
            if (
                repository.get_verification_plan_registration(verification_plan_id)
                is None
            ):
                raise InstallationAuthorityError(
                    'installation verification measurement plan lacks its '
                    'persisted registration authority'
                )
        # The persisted lifecycle is part of calibration authority: the whole
        # chain is read through the repository's validated single-head replay.
        events = repository.list_lifecycle_events(plan.plan_id)
        return plan, export, verification, events

    # -- installation datum / cable run resolution ----------------------------

    def _resolve_datum(
        self,
        installation_datum_id: str | None,
        installation_datum_version: str | None,
        *,
        revision: SceneRevision,
    ) -> InstallationDatum | None:
        """Resolve the datum bound to the exact installation target.

        An explicit (id, version) pin loads that exact record; ``None``
        discovers the latest datum version recorded for the document. Every
        resolved datum must pin the resolved SceneRevision — a datum recorded
        against different room geometry fails closed.
        """

        if self.datum_repository is None:
            if installation_datum_id is not None:
                raise InstallationAuthorityError(
                    'installation datum pins require a '
                    'CadInstallationDatumRepository authority'
                )
            return None
        repository = self.datum_repository

        datum: InstallationDatum | None = None
        if installation_datum_id is not None:
            if installation_datum_version is None:
                raise InstallationAuthorityError(
                    'installation datum pins require the exact version'
                )
            datum = repository.get_datum(
                installation_datum_id, installation_datum_version
            )
            if datum is None:
                raise InstallationAuthorityError(
                    f'installation datum does not resolve: '
                    f'{installation_datum_id}/{installation_datum_version}'
                )
        else:
            versions = tuple(
                item
                for item in repository.list_datums(revision.document_id)
            )
            if not versions:
                return None
            latest: dict[str, InstallationDatum] = {}
            for item in versions:
                latest[item.datum_id] = item
            datum = sorted(
                latest.values(), key=lambda item: (item.datum_id, item.version)
            )[-1]

        if datum.document_id != revision.document_id:
            raise InstallationAuthorityError(
                'installation datum belongs to a different document'
            )
        if datum.scene_revision_id != revision.revision_id:
            raise InstallationAuthorityError(
                'installation datum is not bound to the exact installation '
                'SceneRevision'
            )
        return datum

    def _resolve_cable_runs(
        self,
        cable_run_refs: Sequence[InstallationCableRunRef] | None,
        *,
        revision: SceneRevision,
    ) -> tuple[CableRun, ...]:
        """Resolve the cable runs bound to the exact installation target.

        ``None`` discovers the latest recorded version of each run on the
        document (matching treatment discovery); an explicit list pins exact
        (run_id, version) rows. Every resolved run must pin the resolved
        SceneRevision.
        """

        if self.cable_run_repository is None:
            if cable_run_refs:
                raise InstallationAuthorityError(
                    'installation cable-run pins require a '
                    'CadCableRunRepository authority'
                )
            return ()
        repository = self.cable_run_repository

        runs: list[CableRun] = []
        if cable_run_refs is None:
            latest: dict[str, CableRun] = {}
            for item in repository.list_runs(revision.document_id):
                latest[item.run_id] = item
            runs = sorted(
                latest.values(), key=lambda item: (item.run_id, item.version)
            )
        else:
            for ref in cable_run_refs:
                run = repository.get_run(ref.run_id, ref.version)
                if run is None:
                    raise InstallationAuthorityError(
                        f'installation cable run does not resolve: '
                        f'{ref.run_id}/{ref.version}'
                    )
                if (
                    ref.run_sha256 is not None
                    and ref.run_sha256 != run.semantic_sha256
                ):
                    raise InstallationAuthorityError(
                        'installation cable run semantic hash mismatch: '
                        f'{ref.run_id}/{ref.version}'
                    )
                runs.append(run)

        for run in runs:
            if run.document_id != revision.document_id:
                raise InstallationAuthorityError(
                    'installation cable run belongs to a different document'
                )
            if run.scene_revision_id != revision.revision_id:
                raise InstallationAuthorityError(
                    'installation cable run is not bound to the exact '
                    'installation SceneRevision'
                )
        return tuple(runs)

    # -- typed evidence resolution --------------------------------------------

    def _resolve_evidence_ref(
        self,
        ref: InstallationEvidenceRef,
        *,
        revision: SceneRevision,
        variant: SystemVariant | None,
    ) -> None:
        authority = ref.authority
        if authority not in SUPPORTED_INSTALLATION_EVIDENCE_AUTHORITIES:
            raise InstallationAuthorityError(
                'installation evidence authority is not a resolvable stored '
                f'authority: {authority!r}'
            )

        if authority == 'scene_revision':
            resolved = self.scene_repository.get(ref.evidence_id)
            if resolved is None or resolved.document_id != revision.document_id:
                raise InstallationAuthorityError(
                    f'installation evidence SceneRevision does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(ref, resolved.content_hash, 'SceneRevision')
            return

        if authority == 'scene_content_blob':
            if (
                ref.evidence_sha256 is not None
                and ref.evidence_sha256 != ref.evidence_id
            ):
                raise InstallationAuthorityError(
                    'installation evidence scene_content_blob id must be the '
                    'content SHA-256'
                )
            if self.scene_repository.read_blob(ref.evidence_id) is None:
                raise InstallationAuthorityError(
                    f'installation evidence content blob does not resolve: '
                    f'{ref.evidence_id}'
                )
            return

        if authority == 'system_variant':
            repository = self._variant_repository()
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence system_variant requires a '
                    'CadSystemVariantRepository authority'
                )
            resolved = repository.get_variant(ref.evidence_id)
            if resolved is None or resolved.document_id != revision.document_id:
                raise InstallationAuthorityError(
                    f'installation evidence SystemVariant does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(ref, resolved.variant_sha256, 'SystemVariant')
            return

        if authority == 'system_variant.proposal_evidence':
            if variant is None:
                raise InstallationAuthorityError(
                    'installation evidence proposal refs require the resolved '
                    'SystemVariant'
                )
            resolved = next(
                (
                    item
                    for item in variant.proposal_evidence
                    if item.evidence_id == ref.evidence_id
                ),
                None,
            )
            if resolved is None:
                raise InstallationAuthorityError(
                    'installation evidence proposal ref does not exist on the '
                    f'resolved SystemVariant: {ref.evidence_id}'
                )
            self._require_ref_hash(ref, resolved.evidence_sha256, 'proposal evidence')
            if (
                ref.evidence_type is not None
                and ref.evidence_type != resolved.evidence_kind
            ):
                raise InstallationAuthorityError(
                    'installation evidence proposal ref kind mismatch'
                )
            return

        if authority == 'projector_specification':
            repository = self.video_geometry_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence projector_specification requires a '
                    'CadVideoGeometryRepository authority'
                )
            if ref.evidence_sha256 is not None:
                resolved = repository.get_projector_specification_by_hash(
                    ref.evidence_sha256
                )
            elif ref.evidence_type is not None:
                resolved = repository.get_projector_specification(
                    ref.evidence_id,
                    ref.evidence_type,
                )
            else:
                raise InstallationAuthorityError(
                    'installation evidence projector_specification requires an '
                    'exact version (evidence_type) or semantic hash'
                )
            if resolved is None or resolved.specification_id != ref.evidence_id:
                raise InstallationAuthorityError(
                    f'installation evidence ProjectorSpecification does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.specification_sha256, 'ProjectorSpecification'
            )
            if (
                ref.evidence_type is not None
                and ref.evidence_type != resolved.version
            ):
                raise InstallationAuthorityError(
                    'installation evidence ProjectorSpecification version mismatch'
                )
            return

        if authority == 'video_geometry_evaluation':
            repository = self.video_geometry_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence video_geometry_evaluation requires a '
                    'CadVideoGeometryRepository authority'
                )
            resolved = repository.get_evaluation(ref.evidence_id)
            if resolved is None or resolved.evaluation_id != ref.evidence_id:
                raise InstallationAuthorityError(
                    f'installation evidence VideoGeometryEvaluation does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.evaluation_sha256, 'VideoGeometryEvaluation'
            )
            target = resolved.target
            self._require_target_binding(
                revision=revision,
                variant=variant,
                document_id=target.document_id,
                scene_revision_id=target.scene_revision_id,
                scene_content_hash=target.scene_content_hash,
                system_variant_id=target.system_variant_id,
                system_variant_sha256=target.system_variant_sha256,
                label='installation evidence VideoGeometryEvaluation',
            )
            return

        if authority == 'standards_profile':
            repository = self.standards_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence standards_profile requires a '
                    'CadStandardsRepository authority'
                )
            if ref.evidence_type is None:
                raise InstallationAuthorityError(
                    'installation evidence standards_profile requires the exact '
                    'profile version as evidence_type'
                )
            resolved = repository.get_profile(ref.evidence_id, ref.evidence_type)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence StandardsProfile does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.profile_semantic_hash, 'StandardsProfile'
            )
            return

        if authority == 'standards_evaluation':
            repository = self.standards_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence standards_evaluation requires a '
                    'CadStandardsRepository authority'
                )
            resolved = repository.get_evaluation(ref.evidence_id)
            if resolved is None or resolved.evaluation_id != ref.evidence_id:
                raise InstallationAuthorityError(
                    f'installation evidence StandardsEvaluation does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.evaluation_sha256, 'StandardsEvaluation'
            )
            target = resolved.target
            self._require_target_binding(
                revision=revision,
                variant=variant,
                document_id=target.document_id,
                scene_revision_id=target.scene_revision_id,
                scene_content_hash=target.scene_content_hash,
                system_variant_id=target.system_variant_id,
                system_variant_sha256=target.system_variant_sha256,
                label='installation evidence StandardsEvaluation',
            )
            return

        if authority == 'standards_source_authority':
            repository = self.standards_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence standards_source_authority requires '
                    'a CadStandardsRepository authority'
                )
            resolved = repository.get_source_authority(ref.evidence_id)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence StandardsSourceAuthority does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.semantic_hash_sha256, 'StandardsSourceAuthority'
            )
            if (
                ref.evidence_type is not None
                and ref.evidence_type != resolved.authority_version
            ):
                raise InstallationAuthorityError(
                    'installation evidence StandardsSourceAuthority version '
                    'mismatch'
                )
            return

        if authority == 'treatment_evidence':
            repository = self.treatment_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence treatment_evidence requires a '
                    'CadAcousticTreatmentRepository authority'
                )
            resolved = repository.get_evidence(ref.evidence_id)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence TreatmentEvidenceAuthority does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.evidence_sha256, 'TreatmentEvidenceAuthority'
            )
            return

        if authority == 'treatment_definition':
            repository = self.treatment_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence treatment_definition requires a '
                    'CadAcousticTreatmentRepository authority'
                )
            if ref.evidence_type is None:
                raise InstallationAuthorityError(
                    'installation evidence treatment_definition requires the '
                    'exact definition version as evidence_type'
                )
            resolved = repository.get_definition(ref.evidence_id, ref.evidence_type)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence AcousticTreatmentDefinition does '
                    f'not resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.definition_sha256, 'AcousticTreatmentDefinition'
            )
            return

        if authority == 'treatment_placement':
            repository = self.treatment_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence treatment_placement requires a '
                    'CadAcousticTreatmentRepository authority'
                )
            if ref.evidence_type is None or not ref.evidence_type.isdigit():
                raise InstallationAuthorityError(
                    'installation evidence treatment_placement requires the '
                    'exact placement version as evidence_type'
                )
            resolved = repository.get_placement(
                ref.evidence_id,
                int(ref.evidence_type),
            )
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence AcousticTreatmentPlacement does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.placement_sha256, 'AcousticTreatmentPlacement'
            )
            self._require_scene_binding(
                revision=revision,
                document_id=resolved.document_id,
                scene_revision_id=resolved.scene_revision_id,
                scene_content_hash=resolved.scene_content_hash,
                label='installation evidence AcousticTreatmentPlacement',
            )
            if resolved.system_variant_id is not None and (
                variant is None
                or resolved.system_variant_id != variant.variant_id
                or resolved.system_variant_sha256 != variant.variant_sha256
            ):
                raise InstallationAuthorityError(
                    'installation evidence AcousticTreatmentPlacement is not '
                    'bound to the installation SystemVariant'
                )
            return

        if authority == 'calibration_plan':
            repository = self.calibration_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence calibration_plan requires a '
                    'CadCalibrationRepository authority'
                )
            resolved = repository.get_plan(ref.evidence_id)
            if resolved is None or resolved.plan_id != ref.evidence_id:
                raise InstallationAuthorityError(
                    f'installation evidence CalibrationPlan does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.plan_semantic_sha256, 'CalibrationPlan'
            )
            self._require_target_binding(
                revision=revision,
                variant=variant,
                document_id=resolved.document_id,
                scene_revision_id=resolved.scene_revision_id,
                scene_content_hash=resolved.scene_content_hash,
                system_variant_id=resolved.system_variant_id,
                system_variant_sha256=resolved.system_variant_sha256,
                label='installation evidence CalibrationPlan',
            )
            return

        if authority == 'calibration_export':
            repository = self.calibration_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence calibration_export requires a '
                    'CadCalibrationRepository authority'
                )
            resolved = repository.get_export(ref.evidence_id)
            if resolved is None or resolved.export_id != ref.evidence_id:
                raise InstallationAuthorityError(
                    f'installation evidence calibration export does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref,
                resolved.exported_settings_semantic_sha256,
                'calibration export',
            )
            plan = repository.get_plan(resolved.calibration_plan_id)
            if plan is None:
                raise InstallationAuthorityError(
                    'installation evidence calibration export references a '
                    'missing CalibrationPlan'
                )
            self._require_target_binding(
                revision=revision,
                variant=variant,
                document_id=plan.document_id,
                scene_revision_id=plan.scene_revision_id,
                scene_content_hash=plan.scene_content_hash,
                system_variant_id=plan.system_variant_id,
                system_variant_sha256=plan.system_variant_sha256,
                label='installation evidence calibration export',
            )
            return

        if authority == 'calibration_verification_plan':
            repository = self.calibration_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence calibration_verification_plan '
                    'requires a CadCalibrationRepository authority'
                )
            resolved = repository.get_verification_plan(ref.evidence_id)
            if (
                resolved is None
                or resolved.verification_plan_id != ref.evidence_id
            ):
                raise InstallationAuthorityError(
                    'installation evidence verification measurement plan does '
                    f'not resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref,
                resolved.verification_semantic_sha256,
                'verification measurement plan',
            )
            self._require_target_binding(
                revision=revision,
                variant=variant,
                document_id=resolved.document_id,
                scene_revision_id=resolved.scene_revision_id,
                scene_content_hash=resolved.scene_content_hash,
                system_variant_id=resolved.system_variant_id,
                system_variant_sha256=resolved.system_variant_sha256,
                label='installation evidence verification measurement plan',
            )
            return

        if authority == 'measurement':
            repository = self._measurements()
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence measurement requires a '
                    'CadMeasurementRepository authority'
                )
            resolved = repository.get_measurement(ref.evidence_id)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence Measurement does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_scene_binding(
                revision=revision,
                document_id=resolved.document_id,
                scene_revision_id=resolved.scene_revision_id,
                scene_content_hash=resolved.scene_content_hash,
                label='installation evidence Measurement',
            )
            self._require_ref_hash(
                ref, measurement_sha256(resolved), 'Measurement'
            )
            return

        if authority == 'measurement_dataset':
            repository = self._measurements()
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence measurement_dataset requires a '
                    'CadMeasurementRepository authority'
                )
            resolved = repository.get_dataset(ref.evidence_id)
            if resolved is None:
                raise InstallationAuthorityError(
                    f'installation evidence dataset does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(ref, dataset_sha256(resolved), 'dataset')
            measurement = repository.get_measurement(resolved.measurement_id)
            if measurement is None:
                raise InstallationAuthorityError(
                    'installation evidence dataset references a missing '
                    'Measurement'
                )
            self._require_scene_binding(
                revision=revision,
                document_id=measurement.document_id,
                scene_revision_id=measurement.scene_revision_id,
                scene_content_hash=measurement.scene_content_hash,
                label='installation evidence dataset',
            )
            return

        if authority == 'measurement_quality_report':
            repository = self._quality()
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence measurement_quality_report requires '
                    'a CadMeasurementQualityRepository authority'
                )
            resolved = repository.get_report(ref.evidence_id)
            if resolved is None:
                raise InstallationAuthorityError(
                    'installation evidence MeasurementQualityReport does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.report_sha256, 'MeasurementQualityReport'
            )
            self._require_scene_binding(
                revision=revision,
                document_id=resolved.document_id,
                scene_revision_id=resolved.scene_revision_id,
                scene_content_hash=resolved.scene_content_hash,
                label='installation evidence MeasurementQualityReport',
            )
            return

        if authority == 'installation_datum':
            repository = self.datum_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence installation_datum requires a '
                    'CadInstallationDatumRepository authority'
                )
            if ref.evidence_type is None:
                raise InstallationAuthorityError(
                    'installation evidence installation_datum requires the '
                    'exact version as evidence_type'
                )
            resolved = repository.get_datum(ref.evidence_id, ref.evidence_type)
            if resolved is None or resolved.document_id != revision.document_id:
                raise InstallationAuthorityError(
                    f'installation evidence InstallationDatum does not '
                    f'resolve: {ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.semantic_sha256, 'InstallationDatum'
            )
            return

        if authority == 'cable_run':
            repository = self.cable_run_repository
            if repository is None:
                raise InstallationAuthorityError(
                    'installation evidence cable_run requires a '
                    'CadCableRunRepository authority'
                )
            if ref.evidence_type is None:
                raise InstallationAuthorityError(
                    'installation evidence cable_run requires the exact '
                    'version as evidence_type'
                )
            resolved = repository.get_run(ref.evidence_id, ref.evidence_type)
            if resolved is None or resolved.document_id != revision.document_id:
                raise InstallationAuthorityError(
                    f'installation evidence CableRun does not resolve: '
                    f'{ref.evidence_id}'
                )
            self._require_ref_hash(
                ref, resolved.semantic_sha256, 'CableRun'
            )
            return

        raise InstallationAuthorityError(
            'installation evidence authority is not a resolvable stored '
            f'authority: {authority!r}'
        )

    # -- public boundary -------------------------------------------------------

    def build_installation_output_from_authorities(
        self,
        *,
        scene_revision_id: str,
        system_variant_id: str | None = None,
        evidence: Sequence[InstallationEvidenceRef] = (),
        video_geometry_evaluation_id: str | None = None,
        standards_evaluation_id: str | None = None,
        treatment_instances: Sequence[InstallationTreatmentInstanceRef] | None = None,
        calibration_plan_id: str | None = None,
        calibration_export_id: str | None = None,
        calibration_verification_plan_id: str | None = None,
        installation_datum_id: str | None = None,
        installation_datum_version: str | None = None,
        cable_run_refs: Sequence[InstallationCableRunRef] | None = None,
        signal_path_edge_ids: Sequence[str] = (),
    ) -> InstallationOutput:
        """Compile an InstallationOutput purely from replay-validated authority.

        Every section is resolved by exact id through its authoritative
        repository before the pure semantic builder runs:

        - ``scene_revision_id``/``system_variant_id`` resolve through the
          Scene/SystemVariant repositories; the variant must bind to the exact
          resolved revision.
        - ``video_geometry_evaluation_id`` resolves through
          ``CadVideoGeometryRepository.get_evaluation`` (canonical replay) and
          the specification loads by its pinned semantic hash.
        - ``standards_evaluation_id`` resolves through
          ``CadStandardsRepository``, then the persisted evaluation is replayed
          through the canonical evaluator and every published criterion source
          is re-resolved against retained authority.
        - ``treatment_instances`` pins exact placement versions; when ``None``
          (default) every current placement bound to the target is discovered
          from the repository instead. Definitions load through validated
          reads and each host binding is re-evaluated against the bound
          SceneRevision. Pass an empty sequence to force the section UNKNOWN.
        - ``calibration_plan_id`` resolves the plan through
          ``CadCalibrationRepository.get_plan`` (full source-authority and
          support replay); ``calibration_export_id`` and
          ``calibration_verification_plan_id`` pin the exact sub-authorities
          (``None`` means none — never an implicit pick). The persisted
          lifecycle chain is always loaded through the validated single-head
          read, so an export/verification the chain references must be pinned
          or the build fails closed.
        - ``evidence`` refs resolve through the typed authority dispatch:
          ``authority`` names the store, ``evidence_id`` its primary key,
          ``evidence_sha256`` the pinned semantic hash and ``evidence_type``
          the authority version where the key is (id, version). Refs under
          ``system_variant.proposal_evidence`` are derived from the resolved
          variant automatically — callers must not supply them.

        Any missing, foreign or tampered authority raises
        ``InstallationAuthorityError``/``ValueError`` — the report fails closed
        instead of degrading to a misleading section.
        """

        revision = self._resolve_revision(scene_revision_id)
        variant = self._resolve_variant(revision, system_variant_id)

        specification, video_evaluation = self._resolve_video_geometry(
            video_geometry_evaluation_id,
            revision=revision,
            variant=variant,
        )
        profile, standards_evaluation = self._resolve_standards(
            standards_evaluation_id,
            revision=revision,
            variant=variant,
        )
        definitions, placements, surface_evaluations = self._resolve_treatment(
            treatment_instances,
            revision=revision,
            variant=variant,
        )
        plan, export, verification, lifecycle_events = self._resolve_calibration(
            calibration_plan_id,
            calibration_export_id,
            calibration_verification_plan_id,
            revision=revision,
            variant=variant,
        )
        datum = self._resolve_datum(
            installation_datum_id,
            installation_datum_version,
            revision=revision,
        )
        runs = self._resolve_cable_runs(
            cable_run_refs,
            revision=revision,
        )

        evidence_items = tuple(evidence)
        for ref in evidence_items:
            self._resolve_evidence_ref(ref, revision=revision, variant=variant)

        return build_installation_output(
            revision,
            variant=variant,
            evidence=evidence_items,
            projector_specification=specification,
            video_geometry_evaluation=video_evaluation,
            standards_profile=profile,
            standards_evaluation=standards_evaluation,
            treatment_definitions=definitions,
            treatment_placements=placements,
            treatment_surface_evaluations=surface_evaluations,
            calibration_plan=plan,
            calibration_export_snapshot=export,
            verification_measurement_plan=verification,
            calibration_lifecycle_events=lifecycle_events,
            installation_datum=datum,
            cable_runs=runs,
            signal_path_edge_ids=signal_path_edge_ids,
        )

    def latest_calibration_authority_ids(
        self,
        calibration_plan_id: str,
    ) -> tuple[str | None, str | None]:
        """Return the plan's latest persisted ``(export_id, verification_plan_id)``.

        Export order is the append-only repository order; the verification id
        is the latest contract bound to that export. Use the pair as the
        ``calibration_export_id``/``calibration_verification_plan_id`` pins of
        :meth:`build_installation_output_from_authorities` when the report
        should reflect the plan's current persisted chain.
        """

        if self.calibration_repository is None:
            raise InstallationAuthorityError(
                'installation calibration requires a CadCalibrationRepository '
                'authority'
            )
        repository = self.calibration_repository
        exports = repository.list_exports(calibration_plan_id)
        export_id = None if not exports else exports[-1].export_id
        verification_plan_id = None
        if export_id is not None:
            bound = tuple(
                verification
                for verification in repository.list_verification_plans(
                    calibration_plan_id
                )
                if verification.exported_settings_id == export_id
            )
            if bound:
                verification_plan_id = bound[-1].verification_plan_id
        return export_id, verification_plan_id

    def verify_installation_output_replay(
        self,
        output: InstallationOutput,
    ) -> InstallationOutput:
        """Rebuild a persisted/exported InstallationOutput from its authority.

        The model already enforces its own ``semantic_sha256``
        self-consistency; this verifier additionally requires the whole
        snapshot to reproduce from the exact stored authority ids/hashes it
        records — scene revision, variant, section authorities, treatment
        instance pins and evidence refs. Missing or tampered source authority,
        a moved store, or a snapshot that no longer matches canonical reads
        fails closed instead of preserving a misleading ``AVAILABLE`` section.

        Returns the rebuilt output on success.
        """

        if (
            output.schema_version != INSTALLATION_OUTPUT_SCHEMA_VERSION
            or output.authority_version != INSTALLATION_OUTPUT_AUTHORITY_VERSION
        ):
            raise InstallationAuthorityError(
                'InstallationOutput replay requires the current '
                'installation-output authority version'
            )

        video_geometry_evaluation_id = None
        if output.projector is not None and output.projector.status == 'AVAILABLE':
            video_geometry_evaluation_id = (
                output.projector.video_geometry_evaluation_id
            )
            if video_geometry_evaluation_id is None:
                raise InstallationAuthorityError(
                    'AVAILABLE projector summary does not record its '
                    'VideoGeometryEvaluation authority'
                )

        standards_evaluation_id = None
        if output.standards is not None and output.standards.status == 'AVAILABLE':
            standards_evaluation_id = output.standards.evaluation_id
            if standards_evaluation_id is None:
                raise InstallationAuthorityError(
                    'AVAILABLE standards summary does not record its '
                    'StandardsEvaluation authority'
                )

        treatment_instances: tuple[InstallationTreatmentInstanceRef, ...] = ()
        if output.treatment is not None and output.treatment.status == 'AVAILABLE':
            treatment_instances = tuple(
                InstallationTreatmentInstanceRef(
                    instance_id=item.instance_id,
                    placement_version=item.placement_version,
                    placement_sha256=item.placement_sha256,
                )
                for item in output.treatment.instances
            )

        installation_datum_id = None
        installation_datum_version = None
        if output.datum is not None and output.datum.status == 'AVAILABLE':
            installation_datum_id = output.datum.datum_id
            installation_datum_version = output.datum.datum_version
            if installation_datum_id is None or installation_datum_version is None:
                raise InstallationAuthorityError(
                    'AVAILABLE datum summary does not record its '
                    'InstallationDatum authority'
                )

        cable_run_refs: tuple[InstallationCableRunRef, ...] | None = None
        if output.cable_runs is not None and output.cable_runs.runs:
            cable_run_refs = tuple(
                InstallationCableRunRef(
                    run_id=item.run_id,
                    version=item.version,
                    run_sha256=item.run_sha256,
                )
                for item in output.cable_runs.runs
            )
        # Edge presence has no persisted store to re-verify against; replay
        # treats the edges recorded as resolvable at build time as present so
        # the wiring summary reproduces deterministically.
        signal_path_edge_ids = ()
        if output.cable_runs is not None:
            signal_path_edge_ids = tuple(
                item.signal_path_edge_id
                for item in output.cable_runs.runs
                if item.signal_path_edge_id is not None
                and item.freshness_status == 'current'
            )

        calibration_plan_id = None
        calibration_export_id = None
        calibration_verification_plan_id = None
        if (
            output.calibration is not None
            and output.calibration.status == 'AVAILABLE'
        ):
            calibration_plan_id = output.calibration.plan_id
            if calibration_plan_id is None:
                raise InstallationAuthorityError(
                    'AVAILABLE calibration summary does not record its '
                    'CalibrationPlan authority'
                )
            calibration_export_id = output.calibration.export_id
            calibration_verification_plan_id = output.calibration.verification_plan_id

        # The recorded scene/variant binding must still resolve to exactly the
        # same stored authority — a moved or rewritten store fails here before
        # any section replay.
        revision = self._resolve_revision(output.authority.scene_revision_id)
        if (
            revision.document_id != output.authority.document_id
            or revision.content_hash != output.authority.scene_content_hash
        ):
            raise InstallationAuthorityError(
                'InstallationOutput SceneRevision binding does not match the '
                'persisted revision authority'
            )
        variant = self._resolve_variant(
            revision,
            output.authority.system_variant_id,
        )
        if (
            variant is not None
            and variant.variant_sha256 != output.authority.system_variant_sha256
        ):
            raise InstallationAuthorityError(
                'InstallationOutput SystemVariant binding does not match the '
                'persisted variant authority'
            )

        rebuilt = self.build_installation_output_from_authorities(
            scene_revision_id=output.authority.scene_revision_id,
            system_variant_id=output.authority.system_variant_id,
            evidence=tuple(
                item
                for item in output.evidence
                if item.authority not in _DERIVED_EVIDENCE_AUTHORITIES
            ),
            video_geometry_evaluation_id=video_geometry_evaluation_id,
            standards_evaluation_id=standards_evaluation_id,
            treatment_instances=treatment_instances,
            calibration_plan_id=calibration_plan_id,
            calibration_export_id=calibration_export_id,
            calibration_verification_plan_id=calibration_verification_plan_id,
            installation_datum_id=installation_datum_id,
            installation_datum_version=installation_datum_version,
            cable_run_refs=cable_run_refs,
            signal_path_edge_ids=signal_path_edge_ids,
        )
        if rebuilt != output:
            raise InstallationAuthorityError(
                'InstallationOutput does not replay to its recorded semantic '
                f'authority (recorded {output.semantic_sha256}, '
                f'rebuilt {rebuilt.semantic_sha256})'
            )
        return rebuilt
