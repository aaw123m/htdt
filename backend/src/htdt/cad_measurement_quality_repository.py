from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3

from typing import Mapping, get_args

from .cad_ambient_noise import (
    CadAmbientNoiseRepository,
    check_ambient_measurement_compatibility,
)
from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
    ResolvedAuthority,
)
from .cad_measurement_authorities import (
    CadAcousticLevelCalibration,
    CadDatasetLevelReference,
    CadMeasurementTimingReference,
    CadRoutingProfile,
    CadRoutingProfileBinding,
    CadWiringVerificationCheck,
    _validate_calibration_scope_identity,
    _validate_timing_scope_identity,
    absolute_spl_evidence_gaps,
    calibration_applies_to,
    calibration_supports_absolute_spl,
    derive_load_result,
    timing_reference_scope_is_applicable,
    timing_reference_supports_common_timing,
)
from .cad_measurement_stimulus import (
    CadMeasurementExcitationAsset,
    CadMeasurementStimulusProfile,
)
from .cad_measurement_disposition import (
    CadMeasurementCorrection,
    CadMeasurementDisposition,
)
from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementRecord,
    RadiationScope,
    RoutingEvidence,
)
from .cad_listener_pose import (
    CadListenerPoseRepository,
    pose_acoustic_reference_position,
)
from .cad_measurement_targets import CadMeasurementTargetLineage
from .cad_measurement_quality import (
    MACHINE_OBSERVATION_SOURCES,
    OBSERVATION_EVIDENCE_FIELDS,
    OBSERVATION_FIELD_DEFAULTS,
    TIMING_EVIDENCE_FIELDS,
    CadAcquisitionContext,
    CadMeasurementLineageRecord,
    CadMeasurementObservation,
    CadMeasurementQualityReport,
    dataset_sha256,
    measurement_repeatability_rms_db,
    measurement_sha256,
    replay_measurement_quality_report,
)
from .cad_measurement_repository import (
    CadMeasurementRepository,
    VerifiedMeasurementAsset,
)
from .cad_scene import Position3, SceneDocument, acoustic_reference_position
from .cad_schema import (
    check_native_schema_compatibility,
    require_native_tables,
    connect_sqlite,

)
from .managed_assets import (
    ManagedAssetError,
    ManagedAssetStore,
    verify_managed_asset,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


class MeasurementLineageConflictError(ValueError):
    """A retake-lineage save violated the single-head supersession contract."""


@dataclass(frozen=True, slots=True)
class _ReportBindings:
    """Authorities resolved for one report validation pass.

    ``_validate_report_bindings`` produces this so replay re-derives the
    capability matrix from the *resolved* persisted authorities — never from
    report-embedded claims.
    """

    measurement: CadMeasurementRecord
    dataset: CadFrequencyResponseDataset
    acquisition_context: CadAcquisitionContext | None
    observation: CadMeasurementObservation | None
    dataset_level_reference: CadDatasetLevelReference | None
    level_calibration: CadAcousticLevelCalibration | None


class CadMeasurementQualityRepository:
    """Append-only quality and retake evidence over the native N60 measurement authority."""

    def __init__(
        self,
        measurement_repository: CadMeasurementRepository,
        *,
        system_variant_repository=None,
        field_evidence_repository=None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
        listener_pose_repository: CadListenerPoseRepository | None = None,
        pose_evidence_resolver: Callable[[ExactExternalAuthorityRef, str], Position3 | None] | None = None,
    ) -> None:
        self.measurement_repository = measurement_repository
        self.path = Path(measurement_repository.path)
        self.assets_dir = Path(measurement_repository.assets_dir)
        self._asset_store = ManagedAssetStore(self.assets_dir)
        resolvers: dict[str, KindResolver] = {
            'measurement': self._resolve_measurement_kind,
            'stimulus_profile': self._resolve_stimulus_profile_kind,
        }
        resolvers.update(kind_resolvers or {})
        self.resolver = ExactAuthorityResolver(
            measurement_repository.scene_repository,
            system_variant_repository=system_variant_repository,
            field_evidence_repository=field_evidence_repository,
            kind_resolvers=resolvers,
        )
        self.listener_pose_repository = (
            listener_pose_repository
            if listener_pose_repository is not None
            else CadListenerPoseRepository(self.path)
        )
        # Listener poses persist into the same authority database (#632);
        # target-lineage derivation re-validates exact pose pins (#840).
        self._pose_repository = self.listener_pose_repository
        # #863: resolves a correction's pose_evidence_ref into the observed
        # world position the authority records. None fails closed — a
        # spatially-different reassignment without a resolver can never
        # persist.
        self.pose_evidence_resolver = pose_evidence_resolver
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _resolve_measurement_kind(self, ref_id: str) -> ResolvedAuthority | None:
        record = self.measurement_repository.get_measurement(ref_id)
        if record is None:
            return None
        return ResolvedAuthority(
            kind='measurement',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=measurement_sha256(record),
        )

    def _resolve_stimulus_profile_kind(
        self, ref_id: str
    ) -> ResolvedAuthority | None:
        profile = self.get_stimulus_profile(ref_id)
        if profile is None:
            return None
        return ResolvedAuthority(
            kind='stimulus_profile',
            ref_id=ref_id,
            document_id=profile.document_id,
            semantic_sha256=profile.stimulus_profile_sha256,
        )

    def _require_document(self, document_id: str) -> None:
        """Reject records pinning a document with no scene history."""
        if (
            self.measurement_repository.scene_repository
            .most_recently_created_revision(document_id)
            is None
        ):
            raise ValueError(f'unknown document: {document_id}')

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        check_native_schema_compatibility(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_measurement_quality_reports',
                'cad_measurement_lineage',
                'cad_acquisition_contexts',
                'cad_measurement_observations',
                'cad_quality_calibration_files',
                'cad_timing_references',
                'cad_acoustic_level_calibrations',
                'cad_dataset_level_references',
                'cad_routing_profiles',
                'cad_wiring_checks',
                'cad_measurement_target_lineages',
                'cad_measurement_dispositions',
                'cad_measurement_corrections',
                'cad_excitation_assets',
                'cad_stimulus_profiles',
            )

    def _validate_acquisition_context(self, context: CadAcquisitionContext) -> None:
        """Every subject must be an existing measurement in one document.

        When the context binds a persisted ``CadMeasurementTimingReference``
        (#642), the reference must resolve by exact hash and — when the
        context also carries the flat legacy ``timing_reference_id`` — its id
        must equal the authority's id, so the exact timing authority replayed
        is always the one the context declared.
        """
        documents: set[str] = set()
        for subject_id in context.subject_measurement_ids:
            subject = self.measurement_repository.get_measurement(subject_id)
            if subject is None:
                raise ValueError(
                    'acquisition context references unknown subject '
                    f'measurement: {subject_id}'
                )
            documents.add(subject.document_id)
        if len(documents) != 1:
            raise ValueError(
                'acquisition context subjects must belong to one document'
            )
        document_id = next(iter(documents))
        if context.timing_reference_sha256 is not None:
            reference = self._find_timing_reference_by_sha256(
                context.timing_reference_sha256
            )
            if reference is None:
                raise ValueError(
                    'acquisition context binds an unknown timing reference'
                )
            if (
                context.timing_reference_id is not None
                and context.timing_reference_id != reference.timing_reference_id
            ):
                raise ValueError(
                    'acquisition context timing_reference_id does not match '
                    'the bound timing authority'
                )
            self._require_timing_flat_consistency(context, reference)
            # #849/#860: a matching hash alone is not scope proof — every
            # declared scope must actually cover the context's subjects.
            # An 'unknown'-scope reference binds as honest legacy evidence
            # but is never applicable, so it cannot authorize common timing.
            if reference.validity_scope != 'unknown' and not (
                timing_reference_scope_is_applicable(
                    reference,
                    subject_measurement_ids=context.subject_measurement_ids,
                    acquisition_session_id=context.acquisition_session_id,
                    signal_path_identity=context.signal_path_identity,
                    sample_rate_hz=context.sample_rate_hz,
                )
            ):
                raise ValueError(
                    'acquisition context subjects fall outside the bound '
                    'timing reference validity scope'
                )
        if context.routing_profile is not None:
            # #858: the bound routing profile must resolve exactly and —
            # when it declares a document scope — belong to the subjects'
            # document. An unscoped legacy profile resolves but can never
            # prove same-project applicability.
            profile = self.get_routing_profile(
                context.routing_profile.routing_profile_id
            )
            if (
                profile is None
                or profile.routing_profile_sha256
                != context.routing_profile.routing_profile_sha256
            ):
                raise ValueError(
                    'acquisition context binds an unknown routing profile'
                )
            if (
                profile.document_id is not None
                and profile.document_id != document_id
            ):
                raise ValueError(
                    'acquisition context routing profile belongs to a '
                    'different document'
                )

    def _require_timing_flat_consistency(
        self,
        context: CadAcquisitionContext,
        reference: CadMeasurementTimingReference,
    ) -> None:
        """Flat timing fields cannot strengthen what the bound authority declared.

        A context binding a ``CadMeasurementTimingReference`` may only repeat
        values that authority itself attests: ``clock_source`` must name one
        of the authority's declared clock identities (or be absent),
        ``sample_rate_hz`` must equal the authority's rate (and be absent
        when the authority declares none), and ``delay_correction_s`` must
        equal the sum of the authority's typed corrections (absent when the
        authority declares none). A caller-authored value the authority does
        not carry is a contradiction and fails closed.
        """
        if context.clock_source is not None:
            identities = {
                identity
                for identity in (
                    reference.input_clock_identity,
                    reference.output_clock_identity,
                    reference.reference_channel,
                )
                if identity is not None
            }
            if context.clock_source not in identities:
                raise ValueError(
                    'acquisition context clock_source is not attested by '
                    'the bound timing authority'
                )
        if context.sample_rate_hz is not None:
            if (
                reference.sample_rate_hz is None
                or context.sample_rate_hz != reference.sample_rate_hz
            ):
                raise ValueError(
                    'acquisition context sample_rate_hz diverges from the '
                    'bound timing authority'
                )
        if context.delay_correction_s is not None:
            if not reference.delay_corrections:
                raise ValueError(
                    'acquisition context declares a delay correction the '
                    'bound timing authority does not carry'
                )
            declared_total = sum(
                correction.value_s for correction in reference.delay_corrections
            )
            if context.delay_correction_s != declared_total:
                raise ValueError(
                    'acquisition context delay_correction_s diverges from '
                    'the bound timing authority corrections'

                )

    def save_acquisition_context(self, context: CadAcquisitionContext) -> None:
        """Persist an immutable acquisition-context authority.

        Subject measurements are revalidated before the insert and on every
        later resolution, so a context can never attest timing evidence for a
        measurement that does not exist.
        """
        self._validate_acquisition_context(context)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_acquisition_contexts WHERE acquisition_context_id=?',
                (context.acquisition_context_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'acquisition context already exists: {context.acquisition_context_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_acquisition_contexts(
                    acquisition_context_id, acquisition_context_sha256,
                    source_kind, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    context.acquisition_context_id,
                    context.acquisition_context_sha256,
                    context.source_kind,
                    context.created_at_utc,
                    context.model_dump_json(),
                ),
            )

    def get_acquisition_context(
        self,
        acquisition_context_id: str,
    ) -> CadAcquisitionContext | None:
        """Resolve a persisted context, re-validating its subject bindings."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acquisition_contexts WHERE acquisition_context_id=?',
                (acquisition_context_id,),
            ).fetchone()
        if row is None:
            return None
        context = CadAcquisitionContext.model_validate_json(row['payload_json'])
        self._validate_acquisition_context(context)
        return context

    def list_acquisition_contexts(
        self,
    ) -> tuple[CadAcquisitionContext, ...]:
        """Every persisted acquisition context, newest first.

        Used as reusable acquisition-condition presets (#471): a later
        measurement builds a *new* context seeded from an existing one's
        capture fields; the persisted row is never mutated.
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acquisition_contexts '
                'ORDER BY created_at_utc DESC, acquisition_context_id'
            ).fetchall()
        contexts: list[CadAcquisitionContext] = []
        for row in rows:
            context = CadAcquisitionContext.model_validate_json(row['payload_json'])
            self._validate_acquisition_context(context)
            contexts.append(context)
        return tuple(contexts)

    def _validate_observation(self, observation: CadMeasurementObservation) -> None:
        """The subject must exist; machine observations pin its raw asset."""
        subject = self.measurement_repository.get_measurement(
            observation.measurement_id
        )
        if subject is None:
            raise ValueError(
                'measurement observation references unknown measurement: '
                f'{observation.measurement_id}'
            )
        if observation.source_kind in MACHINE_OBSERVATION_SOURCES:
            # The pinned artifact must be the subject's own verified raw
            # asset: the authoritative dataset read re-runs the managed
            # asset contract and the pinned importer replay, so an
            # observation cannot claim metadata extracted from foreign or
            # fabricated bytes.
            dataset = self.measurement_repository.dataset_for_measurement(
                observation.measurement_id
            )
            if dataset is None:
                raise ValueError(
                    'machine-derived observation requires the subject '
                    'measurement dataset'
                )
            if observation.source_asset_sha256 != dataset.source_sha256:
                # An IR-derived observation pins the measurement's impulse-
                # response raw asset instead of the FR raw asset (#474).
                ir_match = any(
                    ir_dataset.source_sha256 == observation.source_asset_sha256
                    for ir_dataset in self.measurement_repository.ir_datasets_for_measurement(
                        observation.measurement_id
                    )
                )
                if not ir_match:
                    raise ValueError(
                        'machine-derived observation must pin the subject '
                        'measurement raw asset'
                    )

    def save_observation(self, observation: CadMeasurementObservation) -> None:
        """Persist an immutable measurement-observation authority."""
        self._validate_observation(observation)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_observations WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'measurement observation already exists: {observation.observation_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_measurement_observations(
                    observation_id, measurement_id, observation_sha256,
                    source_kind, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    observation.observation_id,
                    observation.measurement_id,
                    observation.observation_sha256,
                    observation.source_kind,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self,
        observation_id: str,
    ) -> CadMeasurementObservation | None:
        """Resolve a persisted observation, re-validating its subject binding."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_observations WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadMeasurementObservation.model_validate_json(row['payload_json'])
        self._validate_observation(observation)
        return observation

    def list_observations(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementObservation, ...]:
        """Return every persisted observation authority for one measurement."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_measurement_observations
                WHERE measurement_id=?
                ORDER BY seq ASC
                ''',
                (measurement_id,),
            ).fetchall()
        observations = tuple(
            CadMeasurementObservation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for observation in observations:
            self._validate_observation(observation)
        return observations

    def save_calibration_file(self, *, filename: str, raw_bytes: bytes) -> str:
        """Retain a calibration file as a content-addressed managed asset.

        Returns the file's SHA-256 — the only hash a report may claim as
        applied or expected calibration authority. The bytes live in the
        shared managed asset store and the registry row mirrors the
        ``cad_measurement_assets`` contract, so ``validate_calibration_file``
        re-proves path containment, size and content hash on every
        resolution.
        """
        if not filename:
            raise ValueError('calibration filename must not be empty')
        digest = sha256(raw_bytes).hexdigest()
        target = self._asset_store.asset_path(digest)
        self._asset_store.ensure_installed(digest, raw_bytes)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT OR IGNORE INTO cad_quality_calibration_files(
                    sha256, filename, relative_path, size_bytes
                ) VALUES (?, ?, ?, ?)''',
                (
                    digest,
                    filename,
                    str(target.relative_to(self.path.parent)),
                    len(raw_bytes),
                ),
            )
        return digest

    def validate_calibration_file(self, digest: str) -> VerifiedMeasurementAsset:
        """Resolve a retained calibration file through the managed asset contract.

        The registry row must exist and the declared file must be a
        contained regular file named by its content address whose stored
        size and streamed SHA-256 match. Any gap raises
        ``ManagedAssetError`` — a typed fail-closed error, never a silent
        pass.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''SELECT sha256, filename, relative_path, size_bytes
                   FROM cad_quality_calibration_files WHERE sha256=?''',
                (digest,),
            ).fetchone()
        if row is None:
            raise ManagedAssetError(
                'calibration file has no retained authority row: '
                f'{digest}'
            )
        sha256_text = str(row['sha256'])
        relative_path = str(row['relative_path'])
        asset_path = verify_managed_asset(
            data_dir=self.path.parent,
            digest=sha256_text,
            relative_path=relative_path,
            size_bytes=int(row['size_bytes']),
            required_root=self.assets_dir,
        )
        if asset_path.name != sha256_text:
            raise ManagedAssetError(
                'calibration file path does not match its content address: '
                f'{relative_path}'
            )
        return VerifiedMeasurementAsset(
            sha256=sha256_text,
            filename=str(row['filename']),
            relative_path=relative_path.replace('\\', '/'),
            size_bytes=int(row['size_bytes']),
            path=asset_path,
        )

    def _resolve_acquisition_context(
        self,
        report: CadMeasurementQualityReport,
        measurement: CadMeasurementRecord,
    ) -> CadAcquisitionContext | None:
        """Resolve the report's context binding against the persisted authority.

        A non-``unknown`` ``source_kind`` string is never proof of existence:
        the binding must resolve to a persisted ``CadAcquisitionContext`` with
        the exact content hash and source kind, covering this measurement, and
        the report's timing evidence must equal the context's attested values
        verbatim. Timing evidence without a bound context — or diverging from
        the resolved one — fails closed. Returns the resolved context so the
        replay can feed scope checks (instrument identity, session scope).
        """
        binding = report.acquisition_context
        if binding is None:
            if any(
                getattr(report.evidence, field) is not None
                for field in TIMING_EVIDENCE_FIELDS
            ):
                raise ValueError(
                    'timing reference evidence requires a bound acquisition context'
                )
            return None
        context = self.get_acquisition_context(binding.acquisition_context_id)
        if context is None:
            raise ValueError(
                'quality report references unknown acquisition context: '
                f'{binding.acquisition_context_id}'
            )
        if context.acquisition_context_sha256 != binding.acquisition_context_sha256:
            raise ValueError('quality report acquisition context hash mismatch')
        if context.source_kind != binding.source_kind:
            raise ValueError(
                'quality report acquisition context source kind mismatch'
            )
        if measurement.measurement_id not in context.subject_measurement_ids:
            raise ValueError(
                'acquisition context does not cover the report measurement'
            )
        for field in TIMING_EVIDENCE_FIELDS:
            if getattr(report.evidence, field) != getattr(context, field):
                raise ValueError(
                    f'timing evidence field {field} diverges from the resolved '
                    'acquisition context'
                )
        if context.timing_reference_sha256 is not None:
            reference = self._find_timing_reference_by_sha256(
                context.timing_reference_sha256
            )
            if reference is None:
                raise ValueError(
                    'acquisition context binds an unknown timing reference'
                )
            if not timing_reference_supports_common_timing(reference):
                # The bound authority cannot witness common timing: no
                # report may claim timing PASS or common_timing ALLOWED
                # from it — whatever its flat fields say.
                if report.timing_reference.status == 'PASS':
                    raise ValueError(
                        'timing reference PASS requires a bound timing '
                        'authority that supports common timing'
                    )
                if report.capability('common_timing').decision == 'ALLOWED':
                    raise ValueError(
                        'common_timing ALLOWED requires a bound timing '
                        'authority that supports common timing'
                    )
        return context

    def _resolve_level_reference(
        self,
        report: CadMeasurementQualityReport,
        measurement: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
    ) -> tuple[CadDatasetLevelReference | None, CadAcousticLevelCalibration | None]:
        """Resolve the report's level-reference pin against persisted authorities (#861).

        When the report pins a ``CadDatasetLevelReference`` the pin must
        resolve to the persisted row for the exact bound dataset — matching
        id, content hash and declared kind (the mirrored kind guard blocks a
        binding that re-labels ``dbfs``/``unknown`` semantics as
        ``absolute_spl``) — and the calibration it binds must resolve with
        the exact pinned hash. A report claiming a level-reference pin for a
        dataset whose persisted reference changed fails closed.
        """
        binding = report.level_reference
        if binding is None:
            return None, None
        reference = self.get_dataset_level_reference(dataset.dataset_id)
        if reference is None:
            raise ValueError(
                'quality report references unknown dataset level reference: '
                f'{binding.level_reference_id}'
            )
        if (
            reference.level_reference_id != binding.level_reference_id
            or reference.level_reference_sha256 != binding.level_reference_sha256
        ):
            raise ValueError('quality report dataset level reference hash mismatch')
        if reference.level_reference_kind != binding.level_reference_kind:
            raise ValueError(
                'quality report dataset level reference kind mismatch'
            )
        if (
            reference.measurement_id != measurement.measurement_id
            or reference.dataset_id != dataset.dataset_id
        ):
            raise ValueError(
                'dataset level reference does not bind this measurement/dataset'
            )
        # The canonical level reference is the source of truth for strong
        # level semantics (#861): the legacy producer-declared dataset field
        # must not contradict it — 'unknown' is a non-claim that never
        # disagrees, while a producer-declared 'dbfs'/'relative' dataset can
        # never be bound to absolute_spl semantics.
        legacy = dataset.level_reference
        if reference.level_reference_kind == 'absolute_spl':
            if legacy not in {'unknown', 'spl', 'absolute_spl'}:
                raise ValueError(
                    'dataset legacy level_reference disagrees with the bound '
                    f'absolute_spl authority: {legacy}'
                )
        elif legacy == 'absolute_spl':
            raise ValueError(
                'dataset legacy level_reference claims absolute SPL but the '
                f'bound authority declares {reference.level_reference_kind}'
            )
        calibration: CadAcousticLevelCalibration | None = None
        if reference.calibration_id is not None:
            calibration = self.get_level_calibration(reference.calibration_id)
            if calibration is None:
                raise ValueError(
                    'dataset level reference resolves to an unknown level '
                    f'calibration: {reference.calibration_id}'
                )
            if calibration.calibration_sha256 != reference.calibration_sha256:
                raise ValueError(
                    'dataset level reference calibration hash mismatch'
                )
        return reference, calibration

    def _resolve_observation(
        self,
        report: CadMeasurementQualityReport,
        measurement: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
    ) -> CadMeasurementObservation | None:
        """Resolve the report's observation binding against the persisted authority.

        Every non-default observation evidence field requires a persisted
        ``CadMeasurementObservation`` whose subject is this exact measurement
        and whose attested values equal the claimed evidence verbatim. The
        report's free-form ``evidence_source`` must equal the resolved
        observation's typed ``source_kind`` (and stay ``unknown`` when no
        observation is bound) so provenance labels can never outrun the
        authority they describe.
        """
        evidence = report.evidence
        binding = report.observation
        if binding is None:
            if any(
                getattr(evidence, field) != OBSERVATION_FIELD_DEFAULTS[field]
                for field in OBSERVATION_EVIDENCE_FIELDS
            ):
                raise ValueError(
                    'observation evidence requires a bound measurement observation'
                )
            if evidence.evidence_source != 'unknown':
                raise ValueError(
                    'evidence_source requires a bound measurement observation'
                )
            return None
        observation = self.get_observation(binding.observation_id)
        if observation is None:
            raise ValueError(
                'quality report references unknown measurement observation: '
                f'{binding.observation_id}'
            )
        if observation.observation_sha256 != binding.observation_sha256:
            raise ValueError('quality report observation hash mismatch')
        if observation.measurement_id != measurement.measurement_id:
            raise ValueError(
                'measurement observation subject does not match the report '
                'measurement'
            )
        if (
            observation.source_kind in MACHINE_OBSERVATION_SOURCES
            and observation.source_asset_sha256 != dataset.source_sha256
        ):
            raise ValueError(
                'machine-derived observation does not resolve to the subject '
                'raw asset'
            )
        for field in OBSERVATION_EVIDENCE_FIELDS:
            if getattr(evidence, field) != getattr(observation, field):
                raise ValueError(
                    f'observation evidence field {field} diverges from the '
                    'resolved measurement observation'
                )
        if evidence.evidence_source != observation.source_kind:
            raise ValueError(
                'evidence_source diverges from the resolved observation authority'
            )
        return observation

    def _resolve_ambient_evidence(
        self,
        report: CadMeasurementQualityReport,
        measurement: CadMeasurementRecord,
        context: CadAcquisitionContext | None,
    ) -> None:
        """Re-verify a bound ambient-noise ref against persisted authority.

        The ambient profile and its operating condition must exist with the
        exact pinned hashes, and the recorded compatibility verdict is
        recomputed against this exact measurement — position, entity,
        revision, acquisition context, operating condition and the
        declared absolute-SPL requirement — so a persisted ref can never
        keep a verdict the evidence no longer supports (#1025).
        """
        ref = report.evidence.ambient_ref
        if ref is None:
            return
        ambient_repository = CadAmbientNoiseRepository(
            self.measurement_repository.scene_repository
        )
        profile = ambient_repository.get_profile(ref.profile_id)
        if profile is None:
            raise ValueError(
                'quality report references unknown ambient profile: '
                f'{ref.profile_id}'
            )
        if profile.profile_sha256 != ref.profile_sha256:
            raise ValueError('ambient noise profile hash mismatch')
        condition = ambient_repository.get_condition(ref.condition_id)
        if condition is None:
            raise ValueError(
                'quality report references unknown ambient operating '
                f'condition: {ref.condition_id}'
            )
        recomputed = check_ambient_measurement_compatibility(
            profile,
            document_id=measurement.document_id,
            scene_revision_id=measurement.scene_revision_id,
            scene_content_hash=measurement.scene_content_hash,
            measurement_entity_id=measurement.measurement_entity_id,
            measurement_position=measurement.measurement_position,
            position_tolerance_m=ref.position_tolerance_m,
            acquisition_context_id=(
                None if context is None else context.acquisition_context_id
            ),
            operating_condition=condition,
            requires_absolute_spl=ref.requires_absolute_spl,
        )
        if recomputed != ref.compatibility:
            raise ValueError(
                'ambient compatibility verdict does not reproduce for this '
                'report'
            )
        if ref.compatibility.status != 'COMPATIBLE' and (
            ref.noise_floor_db_spl is not None or ref.snr_db is not None
        ):
            raise ValueError(
                'non-compatible ambient ref must not carry derived values'
            )

    def _resolve_calibration_authority(
        self,
        report: CadMeasurementQualityReport,
    ) -> None:
        """Every claimed calibration-file hash must resolve to a retained file.

        The calibration check can only PASS when the applied and expected
        hashes match; resolution additionally requires each claimed hash to
        be an exact retained calibration authority that still passes the
        managed asset contract, so provenance can never be invented from a
        bare digest. A descriptive filename must match the retained file's
        registered name.
        """
        evidence = report.evidence
        applied = evidence.calibration_file_sha256
        expected = evidence.expected_calibration_file_sha256
        assets: dict[str, VerifiedMeasurementAsset] = {}
        for digest in {applied, expected} - {None}:
            assets[digest] = self.validate_calibration_file(digest)
        if evidence.calibration_filename is not None:
            if applied is None:
                raise ValueError(
                    'calibration filename requires an applied calibration file hash'
                )
            if assets[applied].filename != evidence.calibration_filename:
                raise ValueError(
                    'calibration filename does not match the retained '
                    'calibration file'
                )

    def _resolve_repeatability(
        self,
        report: CadMeasurementQualityReport,
        repeat_datasets: Sequence[CadFrequencyResponseDataset],
    ) -> None:
        """Recompute the canonical repeatability metric from exact datasets.

        ``repeatability_rms_db`` is never caller-trusted: with two or more
        resolved repeat datasets the persisted value must equal the canonical
        recomputation bit-for-bit; with fewer than two repeats no metric may
        be claimed at all.
        """
        claimed = report.evidence.repeatability_rms_db
        if len(repeat_datasets) >= 2:
            recomputed = measurement_repeatability_rms_db(repeat_datasets)
            if claimed is None or claimed != recomputed:
                raise ValueError(
                    'repeatability RMS diverges from the exact repeat datasets'
                )
        elif claimed is not None:
            raise ValueError(
                'repeatability RMS requires at least two repeat measurements'
            )

    def _validate_report_bindings(
        self,
        report: CadMeasurementQualityReport,
    ) -> _ReportBindings:
        measurement = self.measurement_repository.get_measurement(report.measurement_id)
        if measurement is None:
            raise ValueError(f'quality report references unknown measurement: {report.measurement_id}')
        dataset = self.measurement_repository.get_dataset(report.dataset_id)
        if dataset is None:
            raise ValueError(f'quality report references unknown dataset: {report.dataset_id}')
        if dataset.measurement_id != measurement.measurement_id:
            raise ValueError('quality report dataset/measurement binding mismatch')
        if report.measurement_sha256 != measurement_sha256(measurement):
            raise ValueError('quality report measurement hash mismatch')
        if report.dataset_sha256 != dataset_sha256(dataset):
            raise ValueError('quality report dataset hash mismatch')
        if report.raw_asset_sha256 != dataset.source_sha256:
            raise ValueError('quality report raw asset hash mismatch')
        # The report pins raw_asset_sha256 as its own evidence binding, so
        # the quality gate resolves that exact managed asset through the
        # shared contract (row, containment, regular file, size, SHA-256)
        # rather than relying on the dataset read having checked it moments
        # earlier — a raw asset lost between the two checks still fails
        # closed.
        self.measurement_repository.validate_raw_asset(report.raw_asset_sha256)
        if (
            report.document_id != measurement.document_id
            or report.scene_revision_id != measurement.scene_revision_id
            or report.scene_content_hash != measurement.scene_content_hash
            or report.measurement_entity_id != measurement.measurement_entity_id
            or report.measurement_position != measurement.measurement_position
        ):
            raise ValueError('quality report SceneRevision/entity/measurement-point binding mismatch')

        repeat_datasets: list[CadFrequencyResponseDataset] = []
        for repeat_id in report.evidence.repeat_measurement_ids:
            repeat = self.measurement_repository.get_measurement(repeat_id)
            if repeat is None:
                raise ValueError(f'quality report references unknown repeat measurement: {repeat_id}')
            if (
                repeat.document_id != measurement.document_id
                or repeat.scene_revision_id != measurement.scene_revision_id
                or repeat.scene_content_hash != measurement.scene_content_hash
                or repeat.measurement_entity_id != measurement.measurement_entity_id
                or repeat.measurement_position != measurement.measurement_position
                or repeat.channel_role != measurement.channel_role
                or repeat.source_speaker_ids != measurement.source_speaker_ids
                or repeat.radiation_scope != measurement.radiation_scope
            ):
                raise ValueError('repeatability measurement binding mismatch')
            # The repeatability claim is recomputed from the exact immutable
            # datasets — the authoritative read re-verifies each persisted
            # dataset seal, managed raw asset and importer replay — so a
            # claimed RMS can never outrun the bound evidence.
            repeat_dataset = self.measurement_repository.dataset_for_measurement(
                repeat_id
            )
            if repeat_dataset is None:
                raise ValueError(
                    'repeat measurement has no frequency-response dataset: '
                    f'{repeat_id}'
                )
            repeat_datasets.append(repeat_dataset)

        context = self._resolve_acquisition_context(report, measurement)
        observation = self._resolve_observation(report, measurement, dataset)
        self._resolve_ambient_evidence(report, measurement, context)
        self._resolve_calibration_authority(report)
        self._resolve_repeatability(report, repeat_datasets)
        level_reference, level_calibration = self._resolve_level_reference(
            report, measurement, dataset
        )
        return _ReportBindings(
            measurement=measurement,
            dataset=dataset,
            acquisition_context=context,
            observation=observation,
            dataset_level_reference=level_reference,
            level_calibration=level_calibration,
        )

    def _validate_current_report(self, report: CadMeasurementQualityReport) -> None:
        bindings = self._validate_report_bindings(report)
        rebuilt = replay_measurement_quality_report(
            report,
            measurement=bindings.measurement,
            dataset=bindings.dataset,
            dataset_level_reference=bindings.dataset_level_reference,
            level_calibration=bindings.level_calibration,
            acquisition_context_record=bindings.acquisition_context,
            observation_record=bindings.observation,
        )
        if rebuilt != report:
            raise ValueError('quality report does not match canonical quality algorithm output')

    def save_report(self, report: CadMeasurementQualityReport) -> None:
        self._validate_current_report(report)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_measurement_quality_reports WHERE report_id=?',
                (report.report_id,),
            ).fetchone() is not None:
                raise ValueError(f'quality report already exists: {report.report_id}')
            connection.execute(
                '''
                INSERT INTO cad_measurement_quality_reports(
                    report_id, measurement_id, dataset_id, raw_asset_sha256,
                    report_sha256, profile_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    report.report_id,
                    report.measurement_id,
                    report.dataset_id,
                    report.raw_asset_sha256,
                    report.report_sha256,
                    report.profile.profile_sha256,
                    report.created_at_utc,
                    report.model_dump_json(),
                ),
            )

    def get_report(self, report_id: str) -> CadMeasurementQualityReport | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_quality_reports WHERE report_id=?',
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        report = CadMeasurementQualityReport.model_validate_json(row['payload_json'])
        self._validate_current_report(report)
        return report

    def list_reports(self, measurement_id: str) -> tuple[CadMeasurementQualityReport, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_measurement_quality_reports
                WHERE measurement_id=?
                ORDER BY seq ASC
                ''',
                (measurement_id,),
            ).fetchall()
        reports = tuple(
            CadMeasurementQualityReport.model_validate_json(row['payload_json'])
            for row in rows
        )
        for report in reports:
            self._validate_current_report(report)
        return reports

    def latest_report(self, measurement_id: str) -> CadMeasurementQualityReport | None:
        reports = self.list_reports(measurement_id)
        return reports[-1] if reports else None

    def _validate_lineage_bindings(self, lineage: CadMeasurementLineageRecord) -> None:
        current = self.measurement_repository.get_measurement(lineage.measurement_id)
        previous = self.measurement_repository.get_measurement(lineage.supersedes_measurement_id)
        selected = self.measurement_repository.get_measurement(lineage.selected_measurement_id)
        if current is None or previous is None or selected is None:
            raise ValueError('measurement lineage references unknown measurement evidence')
        if not (
            current.document_id == previous.document_id == selected.document_id == lineage.document_id
        ):
            raise ValueError('measurement lineage must stay within one document')
        if (
            current.scene_revision_id != previous.scene_revision_id
            or current.scene_content_hash != previous.scene_content_hash
            or current.measurement_entity_id != previous.measurement_entity_id
            or current.measurement_position != previous.measurement_position
            or current.channel_role != previous.channel_role
            or current.source_speaker_ids != previous.source_speaker_ids
            or current.radiation_scope != previous.radiation_scope
        ):
            raise ValueError('retake must preserve the measurement binding it supersedes')

    def save_lineage(self, lineage: CadMeasurementLineageRecord) -> None:
        """Append one retake record extending the single head of its chain.

        The retake history of one measurement binding is an append-only chain
        of ``supersedes_measurement_id -> measurement_id`` edges whose
        topology is enforced under one ``BEGIN IMMEDIATE`` transaction:

        - the superseded measurement must be the current head of its chain —
          a measurement may be superseded at most once, so a second retake
          claiming the same predecessor is rejected as a stale-head fork;
        - the retake measurement must carry no existing lineage edge — it may
          not already supersede a predecessor (which would merge two chains)
          nor already be superseded (which would close a cycle, e.g. B -> A
          after A -> B), so every chain stays a simple path from root to head;
        - ``selected_measurement_id`` stays a per-record decision between the
          two sides of the retake, resolved authoritatively from the chain
          head on read rather than by insertion order.

        Two writers racing to retake the same head cannot both advance it:
        the head check runs inside the write transaction, so the loser sees
        the moved head and fails with ``MeasurementLineageConflictError``.
        The record's measurement bindings are revalidated on every save
        before the lock is taken, so the builder remains a convenience and
        not the only integrity boundary.
        """
        self._validate_lineage_bindings(lineage)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate
            # recheck, the head/topology checks and the insert: concurrent
            # writers cannot both observe the same head.
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_lineage WHERE lineage_id=?',
                (lineage.lineage_id,),
            ).fetchone() is not None:
                raise ValueError(f'measurement lineage already exists: {lineage.lineage_id}')
            prior_child = connection.execute(
                '''
                SELECT measurement_id
                FROM cad_measurement_lineage
                WHERE supersedes_measurement_id=?
                ''',
                (lineage.supersedes_measurement_id,),
            ).fetchone()
            if prior_child is not None:
                raise MeasurementLineageConflictError(
                    f'measurement lineage {lineage.lineage_id} rejected: '
                    f'{lineage.supersedes_measurement_id} is already superseded by '
                    f'{prior_child["measurement_id"]}; a retake must supersede the '
                    'current lineage head'
                )
            bound = connection.execute(
                '''
                SELECT measurement_id, supersedes_measurement_id
                FROM cad_measurement_lineage
                WHERE measurement_id=? OR supersedes_measurement_id=?
                ''',
                (lineage.measurement_id, lineage.measurement_id),
            ).fetchone()
            if bound is not None:
                if bound['measurement_id'] == lineage.measurement_id:
                    reason = (
                        f'already records a retake superseding '
                        f'{bound["supersedes_measurement_id"]}; a measurement may '
                        'supersede at most one predecessor'
                    )
                else:
                    reason = (
                        'is already superseded; reusing it as a retake would '
                        'merge chains or close a lineage cycle'
                    )
                raise MeasurementLineageConflictError(
                    f'measurement lineage {lineage.lineage_id} rejected: '
                    f'{lineage.measurement_id} {reason}'
                )
            connection.execute(
                '''
                INSERT INTO cad_measurement_lineage(
                    lineage_id, document_id, measurement_id, supersedes_measurement_id,
                    selected_measurement_id, lineage_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    lineage.lineage_id,
                    lineage.document_id,
                    lineage.measurement_id,
                    lineage.supersedes_measurement_id,
                    lineage.selected_measurement_id,
                    lineage.lineage_sha256,
                    lineage.created_at_utc,
                    lineage.model_dump_json(),
                ),
            )

    def list_lineage(self, document_id: str) -> tuple[CadMeasurementLineageRecord, ...]:
        """Return the document's retake history as validated single-head chains.

        Rows are replayed in insertion order; every stored column must agree
        with its payload and every record must still bind to its exact
        measurement evidence. Each measurement may supersede at most one
        predecessor and be superseded at most once, so valid history is a set
        of disjoint ``root -> head`` paths. A persisted fork, merge or cycle —
        rows written before the topology contract existed or injected past
        ``save_lineage`` — is surfaced as ``ValueError`` rather than silently
        relying on insertion order.
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT *
                FROM cad_measurement_lineage
                WHERE document_id=?
                ORDER BY seq ASC
                ''',
                (document_id,),
            ).fetchall()
        records: list[CadMeasurementLineageRecord] = []
        children: dict[str, CadMeasurementLineageRecord] = {}
        parents: dict[str, CadMeasurementLineageRecord] = {}
        for row in rows:
            record = CadMeasurementLineageRecord.model_validate_json(row['payload_json'])
            if (
                row['lineage_id'] != record.lineage_id
                or row['document_id'] != record.document_id
                or row['measurement_id'] != record.measurement_id
                or row['supersedes_measurement_id'] != record.supersedes_measurement_id
                or row['selected_measurement_id'] != record.selected_measurement_id
                or row['lineage_sha256'] != record.lineage_sha256
                or row['created_at_utc'] != record.created_at_utc
            ):
                raise ValueError(
                    'persisted measurement lineage row disagrees with its payload'
                )
            self._validate_lineage_bindings(record)
            if record.supersedes_measurement_id in children:
                raise ValueError(
                    'measurement lineage history is not a single-head chain: '
                    f'{record.supersedes_measurement_id} is superseded more than once'
                )
            if record.measurement_id in parents:
                raise ValueError(
                    'measurement lineage history is not a single-head chain: '
                    f'{record.measurement_id} supersedes more than one predecessor'
                )
            children[record.supersedes_measurement_id] = record
            parents[record.measurement_id] = record
            records.append(record)
        # With at most one edge in each direction every component is a simple
        # path that must terminate at an un-superseded head; a component that
        # never reaches one is a persisted cycle.
        for record in records:
            seen = {record.supersedes_measurement_id}
            node = record.measurement_id
            while node in children:
                if node in seen:
                    raise ValueError(
                        'measurement lineage history is not a single-head chain: '
                        f'supersession cycle reaches {node} again'
                    )
                seen.add(node)
                node = children[node].measurement_id
        return tuple(records)

    def selected_measurement_for_lineage(self, measurement_id: str) -> str:
        """Resolve the selected evidence for the retake chain holding *measurement_id*.

        The selection is derived from validated topology, not insertion order:
        ``list_lineage`` proves the component is a single-head chain, the head
        is the unique measurement that was never superseded, and the record
        that produced the head declares the current selection (a retake may
        deliberately keep the superseded side selected). A measurement with
        no lineage resolves to itself.
        """
        measurement = self.measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise KeyError(measurement_id)
        events = self.list_lineage(measurement.document_id)
        children = {event.supersedes_measurement_id: event for event in events}
        parents = {event.measurement_id: event for event in events}
        if measurement_id not in children and measurement_id not in parents:
            return measurement_id
        head = measurement_id
        while head in children:
            head = children[head].measurement_id
        return parents[head].selected_measurement_id

    # ------------------------------------------------------------------
    # Measurement timing references (#642)

    def _find_timing_reference_by_sha256(
        self, timing_reference_sha256: str
    ) -> CadMeasurementTimingReference | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT timing_reference_id FROM cad_timing_references '
                'WHERE timing_reference_sha256=?',
                (timing_reference_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self.get_timing_reference(row['timing_reference_id'])

    def _validate_timing_reference(
        self, reference: CadMeasurementTimingReference
    ) -> None:
        """A scoped reference's declared subjects must resolve (#849/#860).

        ``unknown`` scope carries no enforceable identity — it stays
        readable as legacy evidence but can never authorize common timing.
        """
        _validate_timing_scope_identity(reference)
        for subject_id in reference.subject_measurement_ids:
            if (
                self.measurement_repository.get_measurement(subject_id)
                is None
            ):
                raise ValueError(
                    'timing reference scope subject measurement is '
                    f'unknown: {subject_id}'
                )

    def save_timing_reference(
        self, reference: CadMeasurementTimingReference
    ) -> None:
        """Persist an immutable measurement timing-reference authority."""
        self._validate_timing_reference(reference)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_timing_references WHERE timing_reference_id=?',
                (reference.timing_reference_id,),
            ).fetchone() is not None:
                raise ValueError(
                    'timing reference already exists: '
                    f'{reference.timing_reference_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_timing_references(
                    timing_reference_id, timing_reference_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    reference.timing_reference_id,
                    reference.timing_reference_sha256,
                    reference.model_dump_json(),
                    reference.created_at_utc,
                ),
            )

    def get_timing_reference(
        self, timing_reference_id: str
    ) -> CadMeasurementTimingReference | None:
        """Resolve a persisted timing reference, re-verifying its seal."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT timing_reference_id, timing_reference_sha256, '
                'payload_json, created_at_utc FROM cad_timing_references '
                'WHERE timing_reference_id=?',
                (timing_reference_id,),
            ).fetchone()
        if row is None:
            return None
        reference = CadMeasurementTimingReference.model_validate_json(
            row['payload_json']
        )
        if (
            row['timing_reference_id'] != reference.timing_reference_id
            or row['timing_reference_sha256'] != reference.timing_reference_sha256
            or row['created_at_utc'] != reference.created_at_utc
        ):
            raise ValueError(
                'persisted timing reference row disagrees with its payload'
            )
        return reference

    # ------------------------------------------------------------------
    # Measurement stimulus authorities (#874)

    def _find_stimulus_profile_by_sha256(
        self, stimulus_profile_sha256: str
    ) -> CadMeasurementStimulusProfile | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT stimulus_profile_id FROM cad_stimulus_profiles '
                'WHERE stimulus_profile_sha256=?',
                (stimulus_profile_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self.get_stimulus_profile(row['stimulus_profile_id'])

    def save_excitation_asset(
        self,
        asset: CadMeasurementExcitationAsset,
        content: bytes,
    ) -> None:
        """Admit one managed excitation file and persist its sealed record.

        ``content`` must hash to the declared ``sha256``/``byte_length`` —
        the bytes are installed into the #834 managed asset store before the
        row lands, and every later read re-verifies the file against the
        row, so a persisted asset can never describe missing or mutated
        bytes.
        """
        self._require_document(asset.document_id)
        if sha256(content).hexdigest() != asset.sha256:
            raise ManagedAssetError(
                'excitation asset content does not match its declared sha256'
            )
        if len(content) != asset.byte_length:
            raise ManagedAssetError(
                'excitation asset content does not match its declared '
                'byte_length'
            )
        expected_path = str(
            self._asset_store.asset_path(asset.sha256).relative_to(
                self.path.parent
            )
        ).replace('\\', '/')
        if asset.relative_path.replace('\\', '/') != expected_path:
            raise ManagedAssetError(
                'excitation asset relative_path does not name its '
                f'content-addressed store path: {expected_path}'
            )
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_excitation_assets WHERE excitation_asset_id=?',
                (asset.excitation_asset_id,),
            ).fetchone() is not None:
                raise ValueError(
                    'excitation asset already exists: '
                    f'{asset.excitation_asset_id}'
                )
            self._asset_store.ensure_installed(asset.sha256, content)
            connection.execute(
                '''
                INSERT INTO cad_excitation_assets(
                    excitation_asset_id, document_id, sha256,
                    excitation_sha256, byte_length, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    asset.excitation_asset_id,
                    asset.document_id,
                    asset.sha256,
                    asset.excitation_sha256,
                    asset.byte_length,
                    asset.model_dump_json(),
                    asset.created_at_utc,
                ),
            )

    def get_excitation_asset(
        self, excitation_asset_id: str
    ) -> CadMeasurementExcitationAsset | None:
        """Resolve a persisted excitation asset, re-verifying row, seal and
        managed bytes."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT excitation_asset_id, document_id, sha256, '
                'excitation_sha256, byte_length, payload_json, created_at_utc '
                'FROM cad_excitation_assets WHERE excitation_asset_id=?',
                (excitation_asset_id,),
            ).fetchone()
        if row is None:
            return None
        asset = CadMeasurementExcitationAsset.model_validate_json(
            row['payload_json']
        )
        if (
            row['excitation_asset_id'] != asset.excitation_asset_id
            or row['document_id'] != asset.document_id
            or row['sha256'] != asset.sha256
            or row['excitation_sha256'] != asset.excitation_sha256
            or row['byte_length'] != asset.byte_length
            or row['created_at_utc'] != asset.created_at_utc
        ):
            raise ValueError(
                'persisted excitation asset row disagrees with its payload'
            )
        verify_managed_asset(
            data_dir=self.path.parent,
            digest=asset.sha256,
            relative_path=asset.relative_path,
            size_bytes=asset.byte_length,
            required_root=self.assets_dir,
        )
        return asset

    def _validate_stimulus_profile(
        self, profile: CadMeasurementStimulusProfile
    ) -> None:
        self._require_document(profile.document_id)
        if profile.excitation_asset is not None:
            asset = self.get_excitation_asset(
                profile.excitation_asset.excitation_asset_id
            )
            if asset is None:
                raise ValueError(
                    'stimulus profile binds an unknown excitation asset: '
                    f'{profile.excitation_asset.excitation_asset_id}'
                )
            if asset.document_id != profile.document_id:
                raise ValueError(
                    'stimulus profile excitation asset belongs to a '
                    'different document'
                )
            if (
                asset.excitation_sha256
                != profile.excitation_asset.excitation_sha256
                or asset.sha256 != profile.excitation_asset.sha256
            ):
                raise ValueError(
                    'stimulus profile excitation asset binding hash mismatch'
                )
        if profile.measurement_dataset_sha256 is not None:
            dataset = self._find_dataset_by_sha256(
                profile.measurement_dataset_sha256
            )
            if dataset is None:
                raise ValueError(
                    'stimulus profile binds an unknown measurement dataset'
                )
            record = self.measurement_repository.get_measurement(
                dataset.measurement_id
            )
            if record is None or record.document_id != profile.document_id:
                raise ValueError(
                    'stimulus profile measurement dataset belongs to a '
                    'different document'
                )

    def _find_dataset_by_sha256(
        self, dataset_sha256_value: str
    ) -> CadFrequencyResponseDataset | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT dataset_id FROM cad_frequency_responses '
                'WHERE dataset_sha256=?',
                (dataset_sha256_value,),
            ).fetchone()
        if row is None:
            return None
        return self.measurement_repository.get_dataset(row['dataset_id'])

    def save_stimulus_profile(
        self, profile: CadMeasurementStimulusProfile
    ) -> None:
        """Persist an immutable stimulus-profile authority.

        Bound excitation assets and source measurement datasets are resolved
        at save: an unresolvable or foreign-document binding is never
        persisted as an exact reference.
        """
        self._validate_stimulus_profile(profile)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_stimulus_profiles WHERE stimulus_profile_id=?',
                (profile.stimulus_profile_id,),
            ).fetchone() is not None:
                raise ValueError(
                    'stimulus profile already exists: '
                    f'{profile.stimulus_profile_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_stimulus_profiles(
                    stimulus_profile_id, document_id,
                    stimulus_profile_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    profile.stimulus_profile_id,
                    profile.document_id,
                    profile.stimulus_profile_sha256,
                    profile.model_dump_json(),
                    profile.created_at_utc,
                ),
            )

    def get_stimulus_profile(
        self, stimulus_profile_id: str
    ) -> CadMeasurementStimulusProfile | None:
        """Resolve a persisted stimulus profile, re-verifying row, seal and
        bound authorities."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT stimulus_profile_id, document_id, '
                'stimulus_profile_sha256, payload_json, created_at_utc '
                'FROM cad_stimulus_profiles WHERE stimulus_profile_id=?',
                (stimulus_profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadMeasurementStimulusProfile.model_validate_json(
            row['payload_json']
        )
        if (
            row['stimulus_profile_id'] != profile.stimulus_profile_id
            or row['document_id'] != profile.document_id
            or row['stimulus_profile_sha256'] != profile.stimulus_profile_sha256
            or row['created_at_utc'] != profile.created_at_utc
        ):
            raise ValueError(
                'persisted stimulus profile row disagrees with its payload'
            )
        self._validate_stimulus_profile(profile)
        return profile

    def list_stimulus_profiles(
        self, document_id: str
    ) -> tuple[CadMeasurementStimulusProfile, ...]:
        """Every persisted stimulus profile for the document, oldest first."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT stimulus_profile_sha256, payload_json '
                'FROM cad_stimulus_profiles '
                'WHERE document_id=? ORDER BY created_at_utc ASC, '
                'stimulus_profile_id',
                (document_id,),
            ).fetchall()
        profiles: list[CadMeasurementStimulusProfile] = []
        for row in rows:
            profile = CadMeasurementStimulusProfile.model_validate_json(
                row['payload_json']
            )
            if row['stimulus_profile_sha256'] != profile.stimulus_profile_sha256:
                raise ValueError(
                    'persisted stimulus profile row disagrees with its payload'
                )
            self._validate_stimulus_profile(profile)
            profiles.append(profile)
        return tuple(profiles)

    # ------------------------------------------------------------------
    # Acoustic level calibrations + dataset level references (#643)

    def _validate_level_calibration(
        self, calibration: CadAcousticLevelCalibration
    ) -> None:
        """Scope + method evidence, and resolvable subjects (#850/#859).

        ``unknown`` scope is honest legacy evidence — it never authorizes
        ``absolute_spl`` because applicability cannot be proven.
        """
        _validate_calibration_scope_identity(calibration)
        if (
            calibration.validity_scope == 'measurement'
            and calibration.subject_measurement_id is not None
            and self.measurement_repository.get_measurement(
                calibration.subject_measurement_id
            )
            is None
        ):
            raise ValueError(
                'measurement-scope calibration subject measurement is '
                f'unknown: {calibration.subject_measurement_id}'
            )

    def _acquisition_contexts_covering(
        self, measurement_id: str
    ) -> tuple[CadAcquisitionContext, ...]:
        """Persisted contexts whose subject list includes the measurement."""
        return tuple(
            context
            for context in self.list_acquisition_contexts()
            if measurement_id in context.subject_measurement_ids
        )

    def _calibration_applies_to_measurement(
        self,
        calibration: CadAcousticLevelCalibration,
        measurement_id: str,
    ) -> bool:
        """Replay calibration applicability against the subject's contexts.

        ``measurement`` scope is a direct identity check; ``session`` and
        ``instrument`` scopes need a persisted acquisition context that
        proves the measurement ran inside the calibration's declared
        session/input chain; ``unknown`` never applies (#850/#859).
        """
        if calibration.validity_scope == 'measurement':
            return calibration_applies_to(
                calibration, measurement_id=measurement_id
            )
        for context in self._acquisition_contexts_covering(measurement_id):
            if calibration_applies_to(
                calibration,
                measurement_id=measurement_id,
                acquisition_session_id=context.acquisition_session_id,
                input_path_identity=context.input_path_identity,
            ):
                return True
        return False

    def save_level_calibration(
        self, calibration: CadAcousticLevelCalibration
    ) -> None:
        """Persist an immutable acoustic level-calibration authority."""
        self._validate_level_calibration(calibration)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_acoustic_level_calibrations '
                'WHERE calibration_id=?',
                (calibration.calibration_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'acoustic level calibration already exists: '
                    f'{calibration.calibration_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_acoustic_level_calibrations(
                    calibration_id, calibration_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    calibration.calibration_id,
                    calibration.calibration_sha256,
                    calibration.model_dump_json(),
                    calibration.calibrated_at_utc,
                ),
            )

    def get_level_calibration(
        self, calibration_id: str
    ) -> CadAcousticLevelCalibration | None:
        """Resolve a persisted acoustic level calibration."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT calibration_sha256, payload_json '
                'FROM cad_acoustic_level_calibrations WHERE calibration_id=?',
                (calibration_id,),
            ).fetchone()
        if row is None:
            return None
        calibration = CadAcousticLevelCalibration.model_validate_json(
            row['payload_json']
        )
        if row['calibration_sha256'] != calibration.calibration_sha256:
            raise ValueError(
                'persisted level calibration row disagrees with its payload'
            )
        return calibration

    def list_level_calibrations(
        self,
    ) -> tuple[CadAcousticLevelCalibration, ...]:
        """Every persisted acoustic level calibration, newest first."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT calibration_sha256, payload_json '
                'FROM cad_acoustic_level_calibrations '
                'ORDER BY created_at_utc DESC, calibration_id'
            ).fetchall()
        calibrations: list[CadAcousticLevelCalibration] = []
        for row in rows:
            calibration = CadAcousticLevelCalibration.model_validate_json(
                row['payload_json']
            )
            if row['calibration_sha256'] != calibration.calibration_sha256:
                raise ValueError(
                    'persisted level calibration row disagrees with its payload'
                )
            calibrations.append(calibration)
        return tuple(calibrations)

    def save_dataset_level_reference(
        self, reference: CadDatasetLevelReference
    ) -> None:
        """Persist the typed level-semantics binding for one exact dataset.

        The bound dataset must exist with its persisted semantic hash, and
        an ``absolute_spl`` reference must resolve a persisted calibration
        authority that actually supports absolute SPL — a mic response
        file, manufacturer sensitivity sheet or manual entry never
        authorizes it.
        """
        dataset = self.measurement_repository.get_dataset(reference.dataset_id)
        if dataset is None:
            raise ValueError(
                'dataset level reference binds an unknown dataset: '
                f'{reference.dataset_id}'
            )
        if dataset.measurement_id != reference.measurement_id:
            raise ValueError(
                'dataset level reference measurement binding mismatch'
            )
        if dataset.dataset_sha256 != reference.dataset_sha256:
            raise ValueError(
                'dataset level reference does not match the persisted '
                'dataset identity'
            )
        if reference.calibration_id is not None:
            calibration = self.get_level_calibration(reference.calibration_id)
            if (
                calibration is None
                or calibration.calibration_sha256
                != reference.calibration_sha256
            ):
                raise ValueError(
                    'dataset level reference binds an unknown calibration'
                )
            if reference.level_reference_kind == 'absolute_spl':
                gaps = absolute_spl_evidence_gaps(calibration)
                if gaps:
                    raise ValueError(
                        'bound calibration does not authorize absolute SPL '
                        f'— missing evidence: {", ".join(gaps)}'
                    )
            if (
                reference.level_reference_kind == 'absolute_spl'
                and not self._calibration_applies_to_measurement(
                    calibration, reference.measurement_id
                )
            ):
                # #850/#859: method capability is necessary but not
                # sufficient — the calibration's declared scope must cover
                # this exact measurement's acquisition chain. A dataset
                # whose applicability cannot be proven stays uncalibrated.
                raise ValueError(
                    'bound calibration scope does not cover this '
                    'measurement acquisition — absolute SPL requires '
                    'proven applicability'
                )
            if reference.level_reference_kind == 'absolute_spl':
                self._verify_absolute_spl_applicability(
                    reference, calibration
                )
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_dataset_level_references '
                'WHERE level_reference_id=? OR dataset_id=?',
                (reference.level_reference_id, reference.dataset_id),
            ).fetchone() is not None:
                raise ValueError(
                    'dataset level reference already exists: '
                    f'{reference.level_reference_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_dataset_level_references(
                    level_reference_id, dataset_id,
                    level_reference_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    reference.level_reference_id,
                    reference.dataset_id,
                    reference.level_reference_sha256,
                    reference.model_dump_json(),
                    reference.created_at_utc,
                ),
            )

    def _verify_absolute_spl_applicability(
        self,
        reference: CadDatasetLevelReference,
        calibration: CadAcousticLevelCalibration,
    ) -> None:
        """Cross-check the calibration's declared input chain (#827).

        Beyond scope replay, input-chain fields declared on both the
        calibration and a covering acquisition context must agree: a
        calibration taken on a different input device or microphone chain
        never transfers silently. Fields declared on only one side are
        documented but not contradicted.
        """
        contexts = self._acquisition_contexts_covering(
            reference.measurement_id
        )
        mismatches = [
            mismatch
            for context in contexts
            if (mismatch := self._calibration_context_mismatch(calibration, context))
            is not None
        ]
        if mismatches and len(mismatches) == len(contexts):
            raise ValueError(
                'bound calibration does not apply to the dataset '
                f'acquisition context: {mismatches[0]}'
            )

    @staticmethod
    def _calibration_context_mismatch(
        calibration: CadAcousticLevelCalibration,
        context: CadAcquisitionContext,
    ) -> str | None:
        microphone = context.microphone
        if (
            calibration.input_device_label is not None
            and microphone is not None
            and microphone.connection is not None
            and calibration.input_device_label != microphone.connection
        ):
            return (
                'calibration input device differs from the acquisition '
                'input device'
            )
        if (
            calibration.method
            in ('acoustic_calibrator', 'rew_spl_session')
            and calibration.instrument_identity is not None
            and microphone is not None
        ):
            mic_identities = {
                value
                for value in (
                    microphone.serial,
                    microphone.model,
                    microphone.manufacturer,
                )
                if value
            }
            if (
                mic_identities
                and calibration.instrument_identity not in mic_identities
            ):
                return (
                    'calibration instrument identity does not match the '
                    'acquisition microphone'
                )
        return None

    def get_dataset_level_reference(
        self, dataset_id: str
    ) -> CadDatasetLevelReference | None:
        """Resolve the persisted level reference for an exact dataset.

        Re-validates the dataset identity and the bound calibration — a
        persisted row whose calibration vanished or no longer matches is
        rejected rather than silently downgraded.
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_dataset_level_references '
                'WHERE dataset_id=?',
                (dataset_id,),
            ).fetchone()
        if row is None:
            return None
        reference = CadDatasetLevelReference.model_validate_json(
            row['payload_json']
        )
        dataset = self.measurement_repository.get_dataset(reference.dataset_id)
        if dataset is None or dataset.dataset_sha256 != reference.dataset_sha256:
            raise ValueError(
                'persisted dataset level reference no longer matches its '
                'dataset identity'
            )
        if reference.calibration_id is not None:
            calibration = self.get_level_calibration(reference.calibration_id)
            if (
                calibration is None
                or calibration.calibration_sha256
                != reference.calibration_sha256
            ):
                raise ValueError(
                    'persisted dataset level reference calibration is '
                    'unresolvable'
                )
        return reference

    # ------------------------------------------------------------------
    # Verified routing profiles (#473)

    def _validate_routing_profile(
        self,
        profile: CadRoutingProfile,
    ) -> None:
        """Project/topology scope + speaker identity validation (#848/#858).

        A profile must declare a document or revision scope at save — an
        unscoped record can never prove applicability later. A profile
        pinned to ``scene_revision_id`` resolves that exact immutable
        revision (whose document must equal ``document_id`` when both are
        declared); a document-only profile validates against the document's
        current head. Runs at save; on read it re-runs only for pinned
        revisions — a document-scoped profile remains inspectable history
        after the head topology moves.
        """

        if profile.document_id is None and profile.scene_revision_id is None:
            raise ValueError(
                'routing profile requires a document_id or '
                'scene_revision_id scope'
            )
        scene_repository = self.measurement_repository.scene_repository
        revision = None
        if profile.scene_revision_id is not None:
            revision = scene_repository.get(profile.scene_revision_id)
            if revision is None:
                raise ValueError(
                    'routing profile scope revision is unresolvable: '
                    f'{profile.scene_revision_id}'
                )
            if (
                profile.document_id is not None
                and revision.document_id != profile.document_id
            ):
                raise ValueError(
                    'routing profile scope revision is unresolvable for '
                    f'document {profile.document_id}: document does not '
                    'match the pinned scene revision'
                )
        elif profile.document_id is not None:
            revision = scene_repository.current_head(profile.document_id)
            if revision is None:
                raise ValueError(
                    'routing profile belongs to an unknown document: '
                    f'{profile.document_id}'
                )
        if revision is None:
            return
        for entry in profile.entries:
            for speaker_id in (
                *entry.expected_speaker_ids,
                *entry.observed_speaker_ids,
            ):
                try:
                    entity = revision.document.entity(speaker_id)
                except KeyError as exc:
                    raise ValueError(
                        'routing profile speaker id is missing from the '
                        f'bound scene: {speaker_id}'
                    ) from exc
                if entity.kind != 'speaker':
                    raise ValueError(
                        'routing profile source is not a speaker entity: '
                        f'{speaker_id}'
                    )


    def save_routing_profile(self, profile: CadRoutingProfile) -> None:
        """Persist an immutable verified channel-map authority."""
        self._validate_routing_profile(profile)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_routing_profiles WHERE routing_profile_id=?',
                (profile.routing_profile_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'routing profile already exists: '
                    f'{profile.routing_profile_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_routing_profiles(
                    routing_profile_id, routing_profile_sha256,
                    profile_name, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    profile.routing_profile_id,
                    profile.routing_profile_sha256,
                    profile.profile_name,
                    profile.model_dump_json(),
                    profile.created_at_utc,
                ),
            )

    def get_routing_profile(
        self, routing_profile_id: str
    ) -> CadRoutingProfile | None:
        """Resolve a persisted routing profile by exact id."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT routing_profile_sha256, payload_json '
                'FROM cad_routing_profiles WHERE routing_profile_id=?',
                (routing_profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadRoutingProfile.model_validate_json(row['payload_json'])
        if row['routing_profile_sha256'] != profile.routing_profile_sha256:
            raise ValueError(
                'persisted routing profile row disagrees with its payload'
            )
        # Re-verify only the immutable pinned-revision binding: its scene is
        # deterministic forever. A document-scoped profile stays inspectable
        # history even after the head topology moves or the document goes
        # away — save-time validation already ran once.
        if profile.scene_revision_id is not None:
            self._validate_routing_profile(profile)
        return profile

    def list_routing_profiles(
        self, document_id: str | None = None
    ) -> tuple[CadRoutingProfile, ...]:
        """Persisted routing profiles, oldest first.

        With ``document_id`` only profiles bound to that exact project
        document are returned — an unscoped or foreign-document profile is
        never presented as selectable authority for this document (#858).
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT routing_profile_sha256, payload_json '
                'FROM cad_routing_profiles ORDER BY created_at_utc ASC, '
                'routing_profile_id'
            ).fetchall()
        profiles: list[CadRoutingProfile] = []
        for row in rows:
            profile = CadRoutingProfile.model_validate_json(row['payload_json'])
            if row['routing_profile_sha256'] != profile.routing_profile_sha256:
                raise ValueError(
                    'persisted routing profile row disagrees with its payload'
                )
            if (
                document_id is not None
                and profile.document_id != document_id
            ):
                continue
            profiles.append(profile)
        return tuple(profiles)

    # ------------------------------------------------------------------
    # Speaker wiring commissioning checks (#645)

    def _resolve_wiring_check(self, check: CadWiringVerificationCheck) -> None:
        """Prove every pinned authority and claimed reference resolves (#825).

        The check must pin an exact ``SceneRevision`` (id + content hash) of
        the same document, every speaker id in the three speaker tuples must
        resolve to a ``speaker`` entity inside that pinned revision, a bound
        ``SystemVariant`` and every typed ``evidence_ref`` must resolve
        same-document with exact hashes, and a ``routing`` check's optional
        profile binding must resolve to a persisted ``CadRoutingProfile``
        whose matching channel-map entry is applicable to the pinned scene.
        """
        revision = self.measurement_repository.scene_repository.get(
            check.scene_revision_id
        )
        if revision is None:
            raise ValueError(
                'wiring check pins an unknown scene revision: '
                f'{check.scene_revision_id}'
            )
        if revision.document_id != check.document_id:
            raise ValueError(
                'wiring check scene revision belongs to a different document'
            )
        if revision.content_hash != check.scene_revision_sha256:
            raise ValueError(
                'wiring check scene revision hash mismatch'
            )
        for label, speaker_ids in (
            ('expected_speaker_ids', check.expected_speaker_ids),
            ('source_speaker_ids', check.source_speaker_ids),
            ('observed_speaker_ids', check.observed_speaker_ids),
        ):
            for speaker_id in speaker_ids:
                try:
                    entity = revision.document.entity(speaker_id)
                except KeyError:
                    raise ValueError(
                        f'wiring check {label} names an entity outside the '
                        f'pinned scene revision: {speaker_id}'
                    ) from None
                if entity.kind != 'speaker':
                    raise ValueError(
                        f'wiring check {label} names a non-speaker entity: '
                        f'{speaker_id}'
                    )
        if check.system_variant_id is not None:
            self.resolver.resolve(
                AuthorityRef(
                    kind='system_variant',
                    ref_id=check.system_variant_id,
                    ref_sha256=check.system_variant_sha256,
                ),
                document_id=check.document_id,
            )
        for ref in check.evidence_refs:
            if isinstance(ref, AuthorityRef):
                self.resolver.resolve(ref, document_id=check.document_id)
        if check.routing_profile is not None:
            profile = self.get_routing_profile(
                check.routing_profile.routing_profile_id
            )
            if profile is None:
                raise ValueError(
                    'wiring check binds an unknown routing profile: '
                    f'{check.routing_profile.routing_profile_id}'
                )
            if (
                profile.routing_profile_sha256
                != check.routing_profile.routing_profile_sha256
            ):
                raise ValueError('wiring check routing profile hash mismatch')
            if check.expected_output_reference is not None:
                entry = next(
                    (
                        candidate
                        for candidate in profile.entries
                        if check.expected_output_reference
                        in (
                            candidate.rew_channel_label,
                            candidate.logical_role,
                            candidate.output_device_label,
                        )
                    ),
                    None,
                )
                if entry is None:
                    raise ValueError(
                        'wiring check expected_output_reference does not '
                        'resolve to a bound routing profile entry'
                    )
                if (
                    check.expected_speaker_ids
                    and entry.expected_speaker_ids
                    and set(check.expected_speaker_ids)
                    != set(entry.expected_speaker_ids)
                ):
                    raise ValueError(
                        'wiring check expected speakers disagree with the '
                        'bound routing profile entry'
                    )
                for speaker_id in (
                    *entry.expected_speaker_ids,
                    *entry.observed_speaker_ids,
                ):
                    try:
                        entity = revision.document.entity(speaker_id)
                    except KeyError:
                        raise ValueError(
                            'routing profile entry names a speaker outside '
                            f'the pinned scene revision: {speaker_id}'
                        ) from None
                    if entity.kind != 'speaker':
                        raise ValueError(
                            'routing profile entry names a non-speaker '
                            f'entity: {speaker_id}'
                        )

    def save_wiring_check(self, check: CadWiringVerificationCheck) -> None:
        """Persist one independent wiring-verification check result."""
        self._resolve_wiring_check(check)
        self._validate_wiring_check(check)
    def _wiring_check_document(
        self, check: CadWiringVerificationCheck
    ) -> SceneDocument:
        """Resolve the document the check's scene refs are verified in.

        When the check pins a ``scene_revision_id`` the check is judged
        inside that exact as-built baseline; otherwise the document's
        current head is the scope.
        """
        scene_repository = self.measurement_repository.scene_repository
        if check.scene_revision_id is not None:
            revision = scene_repository.get(check.scene_revision_id)
            if revision is None or revision.document_id != check.document_id:
                raise ValueError(
                    'wiring check scene revision is unresolvable for '
                    f'document {check.document_id}'
                )
            return revision.document
        head = scene_repository.current_head(check.document_id)
        if head is None:
            raise ValueError(
                f'wiring check references an unknown document: '
                f'{check.document_id}'
            )
        return head.document

    def _wiring_evidence_document_id(self, reference: str) -> str | None:
        """The document a wiring evidence ref resolves inside, or None.

        Evidence refs name a persisted measurement id, a raw-asset digest
        owned by a measurement in this document, or a measurement
        attachment id/digest — never a free-form string.
        """
        measurement = self.measurement_repository.get_measurement(reference)
        if measurement is not None:
            return measurement.document_id
        if len(reference) == 64:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    '''
                    SELECT m.document_id AS document_id
                    FROM cad_frequency_responses f
                    JOIN cad_measurements m
                        ON m.measurement_id = f.measurement_id
                    WHERE f.source_sha256=?
                    UNION
                    SELECT m.document_id
                    FROM cad_impulse_responses i
                    JOIN cad_measurements m
                        ON m.measurement_id = i.measurement_id
                    WHERE i.source_sha256=?
                    UNION
                    SELECT document_id FROM cad_measurement_attachments
                    WHERE sha256=? OR attachment_id=?
                    ''',
                    (reference, reference, reference, reference),
                ).fetchone()
            if row is not None:
                return str(row['document_id'])
        return None

    def _validate_wiring_check(self, check: CadWiringVerificationCheck) -> None:
        """#848/#857: resolve every claimed speaker/output/evidence ref.

        A wiring check only becomes authority when the document scope
        resolves, every speaker reference names a ``speaker`` entity in the
        check's revision scope, and every evidence ref either resolves to a
        persisted authority in the same document or is an explicit
        ``manual:`` attestation. A PASS claim additionally requires the
        kind-appropriate evidence: routing PASS binds an exact
        RoutingProfile, load PASS binds a typed load observation whose
        derived result is PASS, and other kinds need at least one
        resolvable/attested evidence ref.
        """
        document = self._wiring_check_document(check)
        speaker_ids = {
            speaker_id
            for speaker_id in (
                *check.expected_speaker_ids,
                *check.source_speaker_ids,
                *check.observed_speaker_ids,
            )
        }
        for speaker_id in sorted(speaker_ids):
            try:
                entity = document.entity(speaker_id)
            except KeyError as exc:
                raise ValueError(
                    'wiring check references an unknown speaker entity: '
                    f'{speaker_id}'
                ) from exc
            if entity.kind != 'speaker':
                raise ValueError(
                    'wiring check references a non-speaker entity: '
                    f'{speaker_id}'
                )
        for reference in check.evidence_refs:
            if isinstance(reference, AuthorityRef):
                self.resolver.resolve(
                    reference, document_id=check.document_id
                )
                continue
            if reference.startswith('manual:'):
                if len(reference) <= len('manual:'):
                    raise ValueError(
                        'manual wiring evidence attestation must name what '
                        'was observed'
                    )
                continue
            if (
                self._wiring_evidence_document_id(reference)
                != check.document_id
            ):
                raise ValueError(
                    'wiring check evidence ref is unresolvable in this '
                    f'document: {reference}'
                )
        routing_binding = check.routing_profile_ref or check.routing_profile
        if routing_binding is not None:
            if check.check_kind != 'routing':
                raise ValueError(
                    'a routing profile binding is only valid on a routing '
                    'wiring check'
                )
            profile = self.get_routing_profile(
                routing_binding.routing_profile_id
            )
            if (
                profile is None
                or profile.routing_profile_sha256
                != routing_binding.routing_profile_sha256
            ):
                raise ValueError(
                    'wiring check binds an unresolvable routing profile'
                )
            if (
                profile.document_id is not None
                and profile.document_id != check.document_id
            ):
                raise ValueError(
                    'wiring check routing profile belongs to another '
                    'document'
                )
        if check.load_observation is not None:
            derived = derive_load_result(check.load_observation)
            if check.result != derived:
                raise ValueError(
                    'a load wiring check result must equal the derived '
                    f'result of its bound observation ({derived})'
                )
        if check.result == 'PASS':
            if check.check_kind == 'routing':
                if routing_binding is None:
                    raise ValueError(
                        'a PASS routing check requires an exact routing '
                        'profile binding'
                    )
            elif check.check_kind == 'load':
                if check.load_observation is None:
                    raise ValueError(
                        'a PASS load check requires a typed electrical load '
                        'observation'
                    )
            elif not check.evidence_refs:
                raise ValueError(
                    'a PASS wiring check requires resolvable evidence or '
                    'manual attestation refs'
                )


        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_wiring_checks WHERE check_id=?',
                (check.check_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'wiring verification check already exists: {check.check_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_wiring_checks(
                    check_id, document_id, check_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    check.check_id,
                    check.document_id,
                    check.check_sha256,
                    check.model_dump_json(),
                    check.measured_at_utc,
                ),
            )

    def get_wiring_check(
        self, check_id: str
    ) -> CadWiringVerificationCheck | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT check_sha256, payload_json FROM cad_wiring_checks '
                'WHERE check_id=?',
                (check_id,),
            ).fetchone()
        if row is None:
            return None
        check = CadWiringVerificationCheck.model_validate_json(row['payload_json'])
        if row['check_sha256'] != check.check_sha256:
            raise ValueError(
                'persisted wiring check row disagrees with its payload'
            )
        self._resolve_wiring_check(check)
        return check

    def list_wiring_checks(
        self, document_id: str
    ) -> tuple[CadWiringVerificationCheck, ...]:
        """Every persisted wiring check for the document, oldest first."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT check_sha256, payload_json FROM cad_wiring_checks '
                'WHERE document_id=? ORDER BY created_at_utc ASC, check_id',
                (document_id,),
            ).fetchall()
        checks: list[CadWiringVerificationCheck] = []
        for row in rows:
            check = CadWiringVerificationCheck.model_validate_json(
                row['payload_json']
            )
            if row['check_sha256'] != check.check_sha256:
                raise ValueError(
                    'persisted wiring check row disagrees with its payload'
                )
            self._resolve_wiring_check(check)
            checks.append(check)
        return tuple(checks)

    # ------------------------------------------------------------------
    # Seat-derived measurement target lineage (#472)

    def _validate_target_lineage(
        self, lineage: CadMeasurementTargetLineage
    ) -> None:
        """Prove the seat→point derivation against pinned revisions (#862).

        The sealed hash only proves the payload was not edited after
        construction — it says nothing about whether the derivation ever
        happened. ``creation_revision_id`` is the revision containing the
        created point; the seat was read from that revision's parent
        (explicitly pinned via ``source_scene_revision_id`` when present).
        All entity resolution happens at the pinned revisions, never at
        current head.
        """
        scene_repository = self.measurement_repository.scene_repository
        created = scene_repository.get(lineage.creation_revision_id)
        if created is None:
            raise ValueError(
                'measurement target lineage references unknown creation '
                f'revision: {lineage.creation_revision_id}'
            )
        if created.document_id != lineage.document_id:
            raise ValueError(
                'measurement target lineage creation revision belongs to a '
                'different document'
            )
        if (
            lineage.creation_scene_content_hash is not None
            and lineage.creation_scene_content_hash != created.content_hash
        ):
            raise ValueError(
                'measurement target lineage creation revision content hash '
                'mismatch'
            )

        if lineage.source_scene_revision_id is not None:
            if created.parent_revision_id != lineage.source_scene_revision_id:
                raise ValueError(
                    'measurement target lineage source revision is not the '
                    'creation revision parent'
                )
            source_revision_id = lineage.source_scene_revision_id
        else:
            source_revision_id = created.parent_revision_id
            if source_revision_id is None:
                raise ValueError(
                    'measurement target lineage creation revision has no '
                    'source revision'
                )
        source = scene_repository.get(source_revision_id)
        if source is None:
            raise ValueError(
                'measurement target lineage references unknown source '
                f'revision: {source_revision_id}'
            )
        if source.document_id != lineage.document_id:
            raise ValueError(
                'measurement target lineage source revision belongs to a '
                'different document'
            )
        if (
            lineage.source_scene_content_hash is not None
            and lineage.source_scene_content_hash != source.content_hash
        ):
            raise ValueError(
                'measurement target lineage source revision content hash '
                'mismatch'
            )

        try:
            seat = source.document.entity(lineage.source_seat_id)
        except KeyError as exc:
            raise ValueError(
                'measurement target lineage references unknown source seat '
                f'at its source revision: {lineage.source_seat_id}'
            ) from exc
        if seat.kind != 'seat':
            raise ValueError(
                f'measurement target lineage source entity is not a seat: '
                f'{lineage.source_seat_id}'
            )
        seat_reference = acoustic_reference_position(seat)
        if seat_reference is None:
            raise ValueError(
                f'measurement target lineage source seat '
                f'{lineage.source_seat_id} has no acoustic listener '
                'reference at the pinned source revision'
            )
        if lineage.source_pose_ref is not None:
            pose = self.listener_pose_repository.get_pose(
                lineage.source_pose_ref.authority_id
            )
            if pose is None:
                raise ValueError(
                    'measurement target lineage references unknown listener '
                    f'pose: {lineage.source_pose_ref.authority_id}'
                )
            if pose.authority_ref() != lineage.source_pose_ref:
                raise ValueError(
                    'measurement target lineage listener pose hash mismatch'
                )
            if pose.seat_entity_id != seat.entity_id:
                raise ValueError(
                    'measurement target lineage listener pose is bound to a '
                    'different seat'
                )
            listener_position = pose_acoustic_reference_position(seat, pose)
        else:
            listener_position = seat_reference
        if listener_position != lineage.initial_position:
            raise ValueError(
                'measurement target lineage initial position does not match '
                'the source listener reference at the pinned source revision'
            )

        try:
            point = created.document.entity(lineage.measurement_point_id)
        except KeyError as exc:
            raise ValueError(
                'measurement target lineage references unknown measurement '
                f'point at its creation revision: {lineage.measurement_point_id}'
            ) from exc
        if point.kind != 'measurement_point':
            raise ValueError(
                f'measurement target lineage created entity is not a '
                f'measurement point: {lineage.measurement_point_id}'
            )
        if acoustic_reference_position(point) != lineage.initial_position:
            raise ValueError(
                'measurement target lineage created point position does not '
                'match the declared initial position'
            )

    @staticmethod
    def _validate_target_lineage_row(
        row: sqlite3.Row, lineage: CadMeasurementTargetLineage
    ) -> None:
        """SQL columns must agree with the sealed payload (#313/#847)."""
        if (
            row['target_lineage_id'] != lineage.target_lineage_id
            or row['document_id'] != lineage.document_id
            or row['measurement_point_id'] != lineage.measurement_point_id
            or row['target_lineage_sha256'] != lineage.target_lineage_sha256
            or row['created_at_utc'] != lineage.created_at_utc
        ):
            raise ValueError(
                'persisted target lineage row disagrees with its payload'
            )

    def save_target_lineage(
        self, lineage: CadMeasurementTargetLineage
    ) -> None:
        """Persist a seat-derived measurement-point derivation record.

        The derivation itself is proven against the pinned revisions (#862)
        before the sealed row is written — a self-consistent hash alone is
        never evidence the seat→point relationship existed.
        """
        self._validate_target_lineage(lineage)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_target_lineages '
                'WHERE target_lineage_id=? OR measurement_point_id=?',
                (lineage.target_lineage_id, lineage.measurement_point_id),
            ).fetchone() is not None:
                raise ValueError(
                    'measurement target lineage already exists: '
                    f'{lineage.target_lineage_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_measurement_target_lineages(
                    target_lineage_id, document_id, measurement_point_id,
                    target_lineage_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    lineage.target_lineage_id,
                    lineage.document_id,
                    lineage.measurement_point_id,
                    lineage.target_lineage_sha256,
                    lineage.model_dump_json(),
                    lineage.created_at_utc,
                ),
            )

    def get_target_lineage(
        self, measurement_point_id: str
    ) -> CadMeasurementTargetLineage | None:
        """Resolve the persisted derivation lineage of a measurement point.

        The read re-verifies row columns against the payload and replays
        the derivation in the pinned revision — a corrupt or unverifiable
        historical row fails closed rather than surfacing as provenance.
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT target_lineage_id, document_id, measurement_point_id, '
                'target_lineage_sha256, created_at_utc, payload_json '

                'FROM cad_measurement_target_lineages '
                'WHERE measurement_point_id=?',
                (measurement_point_id,),
            ).fetchone()
        if row is None:
            return None
        lineage = CadMeasurementTargetLineage.model_validate_json(
            row['payload_json']
        )
        # Authoritative reads re-prove the historical derivation (#862) — a
        # persisted row whose seat/revision/point claims no longer resolve
        # fails closed rather than surfacing as exact lineage.
        self._validate_target_lineage_row(row, lineage)
        self._validate_target_lineage(lineage)
        return lineage

    def list_target_lineages(
        self, document_id: str
    ) -> tuple[CadMeasurementTargetLineage, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT target_lineage_id, document_id, measurement_point_id, '
                'target_lineage_sha256, created_at_utc, payload_json '

                'FROM cad_measurement_target_lineages '
                'WHERE document_id=? ORDER BY created_at_utc ASC, '
                'target_lineage_id',
                (document_id,),
            ).fetchall()
        lineages: list[CadMeasurementTargetLineage] = []
        for row in rows:
            lineage = CadMeasurementTargetLineage.model_validate_json(
                row['payload_json']
            )
            self._validate_target_lineage_row(row, lineage)
            self._validate_target_lineage(lineage)
            lineages.append(lineage)
        return tuple(lineages)

    # ------------------------------------------------------------------
    # Disposition / assignment-correction authority (#509)
    # ------------------------------------------------------------------

    def _validate_disposition(self, disposition: CadMeasurementDisposition) -> None:
        subject = self.measurement_repository.get_measurement(
            disposition.measurement_id
        )
        if subject is None:
            raise ValueError(
                'measurement disposition references unknown measurement: '
                f'{disposition.measurement_id}'
            )
        if subject.document_id != disposition.document_id:
            raise ValueError('disposition document does not match the measurement')
        if disposition.correction_id is not None:
            correction = self.get_correction(disposition.correction_id)
            if correction is None or correction.measurement_id != disposition.measurement_id:
                raise ValueError(
                    'corrected disposition must pin a correction record '
                    'of the same measurement'
                )

    def save_disposition(self, disposition: CadMeasurementDisposition) -> None:
        """Append one lifecycle event; the underlying evidence is never rewritten."""
        self._validate_disposition(disposition)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_dispositions WHERE disposition_id=?',
                (disposition.disposition_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'measurement disposition already exists: {disposition.disposition_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_measurement_dispositions(
                    disposition_id, document_id, measurement_id, disposition,
                    correction_id, disposition_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    disposition.disposition_id,
                    disposition.document_id,
                    disposition.measurement_id,
                    disposition.disposition,
                    disposition.correction_id,
                    disposition.disposition_sha256,
                    disposition.created_at_utc,
                    disposition.model_dump_json(),
                ),
            )

    def get_disposition(self, disposition_id: str) -> CadMeasurementDisposition | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_dispositions WHERE disposition_id=?',
                (disposition_id,),
            ).fetchone()
        if row is None:
            return None
        disposition = CadMeasurementDisposition.model_validate_json(
            row['payload_json']
        )
        # Revalidate on read (#863): a 'corrected' disposition whose pinned
        # correction no longer validates must fail closed.
        self._validate_disposition(disposition)
        return disposition

    def list_dispositions(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementDisposition, ...]:
        """Every lifecycle event for one measurement, oldest first."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json FROM cad_measurement_dispositions
                WHERE measurement_id=? ORDER BY seq ASC
                ''',
                (measurement_id,),
            ).fetchall()
        dispositions = tuple(
            CadMeasurementDisposition.model_validate_json(row['payload_json'])
            for row in rows
        )
        for disposition in dispositions:
            self._validate_disposition(disposition)
        return dispositions

    def latest_disposition(
        self,
        measurement_id: str,
    ) -> CadMeasurementDisposition | None:
        """The current lifecycle state of one measurement (None = never set)."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                '''
                SELECT payload_json FROM cad_measurement_dispositions
                WHERE measurement_id=? ORDER BY seq DESC LIMIT 1
                ''',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        disposition = CadMeasurementDisposition.model_validate_json(
            row['payload_json']
        )
        self._validate_disposition(disposition)
        return disposition

    def _validate_correction(self, correction: CadMeasurementCorrection) -> None:
        """A correction must pin the subject's exact bound dataset and valid entities."""
        subject = self.measurement_repository.get_measurement(correction.measurement_id)
        if subject is None:
            raise ValueError(
                'measurement correction references unknown measurement: '
                f'{correction.measurement_id}'
            )
        if subject.document_id != correction.document_id:
            raise ValueError('correction document does not match the measurement')
        dataset = self.measurement_repository.dataset_for_measurement(
            correction.measurement_id
        )
        if dataset is None or dataset.dataset_id != correction.dataset_id:
            raise ValueError('correction must pin the measurement bound dataset')
        if dataset_sha256(dataset) != correction.dataset_sha256:
            raise ValueError('correction dataset hash does not match the bound dataset')
        revision = self.measurement_repository.scene_repository.get(
            subject.scene_revision_id
        )
        if revision is None:
            raise ValueError(
                'measurement source revision is unavailable: '
                f'{subject.scene_revision_id}'
            )
        if correction.measurement_entity_id is not None:
            try:
                entity = revision.document.entity(correction.measurement_entity_id)
            except KeyError as exc:
                raise ValueError(
                    'corrected entity does not exist in the source revision'
                ) from exc
            corrected_position = acoustic_reference_position(entity)
            if corrected_position is None:
                raise ValueError(
                    'corrected entity has no acoustic reference position'
                )
            # #863: a target-entity relabel never rewrites physical truth. When
            # the corrected target's acoustic reference equals the immutable
            # import position exactly, the label swap is provably position-
            # preserving. Any spatial difference requires exact pose evidence
            # pinned by ``pose_evidence_ref`` — never an implicit tolerance.
            if (
                corrected_position != subject.measurement_position
                and correction.measurement_entity_id
                != subject.measurement_entity_id
            ):
                if correction.correction_kind == 'assignment_label_only':
                    raise ValueError(
                        'label-only target correction is not position-'
                        'compatible with the immutable measurement position'
                    )
                if correction.pose_evidence_ref is None:
                    raise ValueError(
                        'target correction to a different position requires '
                        'pinned pose evidence (#732)'
                    )
                resolver = self.pose_evidence_resolver
                if resolver is None:
                    raise ValueError(
                        'no pose-evidence resolver configured — a spatially '
                        'different correction cannot be verified'
                    )
                observed = resolver(
                    correction.pose_evidence_ref, correction.document_id
                )
                if observed is None:
                    raise ValueError(
                        'pose evidence does not resolve to a valid authority '
                        'for this document'
                    )
                if observed != corrected_position:
                    raise ValueError(
                        'resolved pose evidence position does not match the '
                        'corrected target acoustic reference'
                    )
            elif correction.pose_evidence_ref is not None:
                # A pose ref on a position-preserving correction must still
                # resolve — it is claimed authority, never decoration.
                resolver = self.pose_evidence_resolver
                observed = (
                    None
                    if resolver is None
                    else resolver(
                        correction.pose_evidence_ref, correction.document_id
                    )
                )
                if observed is None:
                    raise ValueError(
                        'pose evidence does not resolve to a valid authority '
                        'for this document'
                    )
        if correction.source_speaker_ids is not None:
            for source_id in correction.source_speaker_ids:
                try:
                    source = revision.document.entity(source_id)
                except KeyError as exc:
                    raise ValueError(
                        f'corrected source speaker missing from source revision: '
                        f'{source_id}'
                    ) from exc
                if source.kind != 'speaker':
                    raise ValueError(f'corrected source is not a speaker: {source_id}')
        if (
            correction.radiation_scope is not None
            and correction.radiation_scope not in get_args(RadiationScope)
        ):
            raise ValueError(f'invalid radiation scope: {correction.radiation_scope}')
        if (
            correction.routing_evidence is not None
            and correction.routing_evidence not in get_args(RoutingEvidence)
        ):
            raise ValueError(f'invalid routing evidence: {correction.routing_evidence}')

    def save_correction(self, correction: CadMeasurementCorrection) -> None:
        """Append one corrected binding; the original assignment stays immutable."""
        self._validate_correction(correction)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_corrections WHERE correction_id=?',
                (correction.correction_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'measurement correction already exists: {correction.correction_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_measurement_corrections(
                    correction_id, document_id, measurement_id, dataset_id,
                    dataset_sha256, correction_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    correction.correction_id,
                    correction.document_id,
                    correction.measurement_id,
                    correction.dataset_id,
                    correction.dataset_sha256,
                    correction.correction_sha256,
                    correction.created_at_utc,
                    correction.model_dump_json(),
                ),
            )

    def get_correction(self, correction_id: str) -> CadMeasurementCorrection | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_corrections WHERE correction_id=?',
                (correction_id,),
            ).fetchone()
        if row is None:
            return None
        correction = CadMeasurementCorrection.model_validate_json(
            row['payload_json']
        )
        # Authoritative reads revalidate the exact subject/dataset/entity/
        # pose-evidence bindings (#863) — a row that can no longer prove its
        # corrected binding fails closed instead of elevating as authority.
        self._validate_correction(correction)
        return correction

    def list_corrections(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementCorrection, ...]:
        """Every corrected binding for one measurement, oldest first."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json FROM cad_measurement_corrections
                WHERE measurement_id=? ORDER BY seq ASC
                ''',
                (measurement_id,),
            ).fetchall()
        corrections = tuple(
            CadMeasurementCorrection.model_validate_json(row['payload_json'])
            for row in rows
        )
        for correction in corrections:
            self._validate_correction(correction)
        return corrections

    def latest_correction(
        self,
        measurement_id: str,
    ) -> CadMeasurementCorrection | None:
        """The current effective corrected binding (None = original assignment)."""
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                '''
                SELECT payload_json FROM cad_measurement_corrections
                WHERE measurement_id=? ORDER BY seq DESC LIMIT 1
                ''',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        correction = CadMeasurementCorrection.model_validate_json(
            row['payload_json']
        )
        self._validate_correction(correction)
        return correction
