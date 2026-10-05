"""Append-only persistence for the RP1 performance-facts authority (#586).

Six tables:

* ``cad_performance_fact_profiles`` — sealed ``PerformanceFactsProfile``
  records keyed by ``profile_id``; profile identity (publisher, family,
  revision, maturity) is sealed so a review draft can never be
  re-labelled as final.
* ``cad_performance_fact_products`` — sealed
  ``ManufacturerProductIdentity`` records keyed by ``product_id``.
* ``cad_performance_facts`` — sealed ``PerformanceFact`` records; a
  fact may only be persisted against a stored product whose sha
  matches — engineering data can never float free of the exact
  product/variant identity it describes.
* ``cad_performance_fact_imports`` — sealed ``PerformanceFactsImport``
  runs bound to a stored profile.
* ``cad_performance_fact_evaluations`` — sealed selection verdicts
  bound to a stored product.
* ``cad_performance_fact_rebinds`` — sealed review→final profile
  transitions; both endpoint profiles must be stored.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_performance_facts import (
    ManufacturerProductIdentity,
    PerformanceFact,
    PerformanceFactsEvaluation,
    PerformanceFactsImport,
    PerformanceFactsProfile,
    PerformanceFactsProfileRebind,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class PerformanceFactsConflictError(ValueError):
    """A performance-facts save violated append-only identity rules."""


class PerformanceFactsIntegrityError(ValueError):
    """A stored performance-facts row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise PerformanceFactsIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadPerformanceFactsRepository:
    """Native storage for RP1 performance-facts authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_performance_fact_profiles',
                'cad_performance_fact_products',
                'cad_performance_facts',
                'cad_performance_fact_imports',
                'cad_performance_fact_evaluations',
                'cad_performance_fact_rebinds',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: PerformanceFactsProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise PerformanceFactsConflictError(
                'performance fact profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_fact_profiles (
                    profile_id, profile_sha256, document_id,
                    publisher, family, document_reference,
                    maturity_state, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.publisher,
                    profile.family,
                    profile.document_reference,
                    profile.maturity_state,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> PerformanceFactsProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       publisher, family, document_reference,
                       maturity_state, payload_json
                FROM cad_performance_fact_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self, document_id: str
    ) -> tuple[PerformanceFactsProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       publisher, family, document_reference,
                       maturity_state, payload_json
                FROM cad_performance_fact_profiles
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(self, row: tuple) -> PerformanceFactsProfile:
        (
            profile_id,
            profile_sha256,
            document_id,
            publisher,
            family,
            document_reference,
            maturity_state,
            payload_json,
        ) = row
        profile = PerformanceFactsProfile.model_validate_json(
            payload_json
        )
        if (
            profile.profile_id != profile_id
            or profile.profile_sha256 != profile_sha256
            or profile.document_id != document_id
            or profile.publisher != publisher
            or profile.family != family
            or profile.document_reference != document_reference
            or profile.maturity_state != maturity_state
        ):
            raise PerformanceFactsIntegrityError(
                'performance fact profile row disagrees with payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Products

    def save_product(
        self, product: ManufacturerProductIdentity
    ) -> None:
        _assert_sealed(product, 'product_sha256', 'product_id')
        existing = self.get_product(product.product_id)
        if existing is not None:
            if existing.product_sha256 == product.product_sha256:
                return
            raise PerformanceFactsConflictError(
                'product identities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_fact_products (
                    product_id, product_sha256, document_id,
                    manufacturer, model, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product.product_id,
                    product.product_sha256,
                    product.document_id,
                    product.manufacturer,
                    product.model,
                    product.model_dump_json(),
                ),
            )

    def get_product(
        self, product_id: str
    ) -> ManufacturerProductIdentity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT product_id, product_sha256, document_id,
                       manufacturer, model, payload_json
                FROM cad_performance_fact_products
                WHERE product_id=?
                """,
                (product_id,),
            ).fetchone()
        if row is None:
            return None
        return self._product_from_row(row)

    def list_products(
        self, document_id: str
    ) -> tuple[ManufacturerProductIdentity, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT product_id, product_sha256, document_id,
                       manufacturer, model, payload_json
                FROM cad_performance_fact_products
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._product_from_row(row) for row in rows)

    def _product_from_row(
        self, row: tuple
    ) -> ManufacturerProductIdentity:
        (
            product_id,
            product_sha256,
            document_id,
            manufacturer,
            model,
            payload_json,
        ) = row
        product = ManufacturerProductIdentity.model_validate_json(
            payload_json
        )
        if (
            product.product_id != product_id
            or product.product_sha256 != product_sha256
            or product.document_id != document_id
            or product.manufacturer != manufacturer
            or product.model != model
        ):
            raise PerformanceFactsIntegrityError(
                'product identity row disagrees with payload'
            )
        return product

    # ------------------------------------------------------------------
    # Facts

    def save_fact(self, fact: PerformanceFact) -> None:
        _assert_sealed(fact, 'fact_sha256', 'fact_id')
        product = self.get_product(fact.product_id)
        if product is None or (
            product.product_sha256 != fact.product_sha256
        ):
            raise PerformanceFactsIntegrityError(
                'fact refers to an unstored product identity'
            )
        existing = self.get_fact(fact.fact_id)
        if existing is not None:
            if existing.fact_sha256 == fact.fact_sha256:
                return
            raise PerformanceFactsConflictError(
                'performance facts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_facts (
                    fact_id, fact_sha256, document_id,
                    product_id, product_sha256, quantity_kind,
                    evidence_class, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.fact_id,
                    fact.fact_sha256,
                    fact.document_id,
                    fact.product_id,
                    fact.product_sha256,
                    fact.quantity_kind,
                    fact.evidence_class,
                    fact.model_dump_json(),
                ),
            )

    def get_fact(self, fact_id: str) -> PerformanceFact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT fact_id, fact_sha256, document_id,
                       product_id, product_sha256, quantity_kind,
                       evidence_class, payload_json
                FROM cad_performance_facts
                WHERE fact_id=?
                """,
                (fact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._fact_from_row(row)

    def list_facts(
        self, document_id: str, product_id: str | None = None
    ) -> tuple[PerformanceFact, ...]:
        if product_id is None:
            sql = (
                """
                SELECT fact_id, fact_sha256, document_id,
                       product_id, product_sha256, quantity_kind,
                       evidence_class, payload_json
                FROM cad_performance_facts
                WHERE document_id=?
                ORDER BY seq ASC
                """
            )
            params: tuple = (document_id,)
        else:
            sql = (
                """
                SELECT fact_id, fact_sha256, document_id,
                       product_id, product_sha256, quantity_kind,
                       evidence_class, payload_json
                FROM cad_performance_facts
                WHERE document_id=? AND product_id=?
                ORDER BY seq ASC
                """
            )
            params = (document_id, product_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._fact_from_row(row) for row in rows)

    def _fact_from_row(self, row: tuple) -> PerformanceFact:
        (
            fact_id,
            fact_sha256,
            document_id,
            product_id,
            product_sha256,
            quantity_kind,
            evidence_class,
            payload_json,
        ) = row
        fact = PerformanceFact.model_validate_json(payload_json)
        if (
            fact.fact_id != fact_id
            or fact.fact_sha256 != fact_sha256
            or fact.document_id != document_id
            or fact.product_id != product_id
            or fact.product_sha256 != product_sha256
            or fact.quantity_kind != quantity_kind
            or fact.evidence_class != evidence_class
        ):
            raise PerformanceFactsIntegrityError(
                'performance fact row disagrees with payload'
            )
        return fact

    # ------------------------------------------------------------------
    # Imports

    def save_import(self, run: PerformanceFactsImport) -> None:
        _assert_sealed(run, 'import_sha256', 'import_id')
        profile = self.get_profile(run.profile_id)
        if profile is None or (
            profile.profile_sha256 != run.profile_sha256
        ):
            raise PerformanceFactsIntegrityError(
                'import refers to an unstored profile'
            )
        existing = self.get_import(run.import_id)
        if existing is not None:
            if existing.import_sha256 == run.import_sha256:
                return
            raise PerformanceFactsConflictError(
                'performance fact imports are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_fact_imports (
                    import_id, import_sha256, document_id,
                    profile_id, profile_sha256, extraction_state,
                    imported_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.import_id,
                    run.import_sha256,
                    run.document_id,
                    run.profile_id,
                    run.profile_sha256,
                    run.extraction_state,
                    run.imported_at_utc,
                    run.model_dump_json(),
                ),
            )

    def get_import(
        self, import_id: str
    ) -> PerformanceFactsImport | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT import_id, import_sha256, document_id,
                       profile_id, profile_sha256, extraction_state,
                       imported_at_utc, payload_json
                FROM cad_performance_fact_imports
                WHERE import_id=?
                """,
                (import_id,),
            ).fetchone()
        if row is None:
            return None
        return self._import_from_row(row)

    def list_imports(
        self, document_id: str
    ) -> tuple[PerformanceFactsImport, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT import_id, import_sha256, document_id,
                       profile_id, profile_sha256, extraction_state,
                       imported_at_utc, payload_json
                FROM cad_performance_fact_imports
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._import_from_row(row) for row in rows)

    def _import_from_row(self, row: tuple) -> PerformanceFactsImport:
        (
            import_id,
            import_sha256,
            document_id,
            profile_id,
            profile_sha256,
            extraction_state,
            imported_at_utc,
            payload_json,
        ) = row
        run = PerformanceFactsImport.model_validate_json(payload_json)
        if (
            run.import_id != import_id
            or run.import_sha256 != import_sha256
            or run.document_id != document_id
            or run.profile_id != profile_id
            or run.profile_sha256 != profile_sha256
            or run.extraction_state != extraction_state
            or run.imported_at_utc != imported_at_utc
        ):
            raise PerformanceFactsIntegrityError(
                'performance fact import row disagrees with payload'
            )
        return run

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(
        self, evaluation: PerformanceFactsEvaluation
    ) -> None:
        _assert_sealed(
            evaluation, 'evaluation_sha256', 'evaluation_id'
        )
        product = self.get_product(evaluation.product_id)
        if product is None or (
            product.product_sha256 != evaluation.product_sha256
        ):
            raise PerformanceFactsIntegrityError(
                'evaluation refers to an unstored product identity'
            )
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise PerformanceFactsConflictError(
                'performance fact evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_fact_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    product_id, product_sha256, verdict,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.product_id,
                    evaluation.product_sha256,
                    evaluation.verdict,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> PerformanceFactsEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       product_id, product_sha256, verdict,
                       evaluated_at_utc, payload_json
                FROM cad_performance_fact_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def list_evaluations(
        self, document_id: str
    ) -> tuple[PerformanceFactsEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       product_id, product_sha256, verdict,
                       evaluated_at_utc, payload_json
                FROM cad_performance_fact_evaluations
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def _evaluation_from_row(
        self, row: tuple
    ) -> PerformanceFactsEvaluation:
        (
            evaluation_id,
            evaluation_sha256,
            document_id,
            product_id,
            product_sha256,
            verdict,
            evaluated_at_utc,
            payload_json,
        ) = row
        evaluation = PerformanceFactsEvaluation.model_validate_json(
            payload_json
        )
        if (
            evaluation.evaluation_id != evaluation_id
            or evaluation.evaluation_sha256 != evaluation_sha256
            or evaluation.document_id != document_id
            or evaluation.product_id != product_id
            or evaluation.product_sha256 != product_sha256
            or evaluation.verdict != verdict
            or evaluation.evaluated_at_utc != evaluated_at_utc
        ):
            raise PerformanceFactsIntegrityError(
                'performance fact evaluation row disagrees with payload'
            )
        return evaluation

    # ------------------------------------------------------------------
    # Profile rebinds

    def save_rebind(
        self, rebind: PerformanceFactsProfileRebind
    ) -> None:
        _assert_sealed(rebind, 'rebind_sha256', 'rebind_id')
        from_profile = self.get_profile(rebind.from_profile_id)
        to_profile = self.get_profile(rebind.to_profile_id)
        if from_profile is None or (
            from_profile.profile_sha256 != rebind.from_profile_sha256
        ):
            raise PerformanceFactsIntegrityError(
                'rebind refers to an unstored from-profile'
            )
        if to_profile is None or (
            to_profile.profile_sha256 != rebind.to_profile_sha256
        ):
            raise PerformanceFactsIntegrityError(
                'rebind refers to an unstored to-profile'
            )
        existing = self.get_rebind(rebind.rebind_id)
        if existing is not None:
            if existing.rebind_sha256 == rebind.rebind_sha256:
                return
            raise PerformanceFactsConflictError(
                'performance fact rebinds are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_performance_fact_rebinds (
                    rebind_id, rebind_sha256, document_id,
                    from_profile_id, to_profile_id, decided_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rebind.rebind_id,
                    rebind.rebind_sha256,
                    rebind.document_id,
                    rebind.from_profile_id,
                    rebind.to_profile_id,
                    rebind.decided_at_utc,
                    rebind.model_dump_json(),
                ),
            )

    def get_rebind(
        self, rebind_id: str
    ) -> PerformanceFactsProfileRebind | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT rebind_id, rebind_sha256, document_id,
                       from_profile_id, to_profile_id, decided_at_utc,
                       payload_json
                FROM cad_performance_fact_rebinds
                WHERE rebind_id=?
                """,
                (rebind_id,),
            ).fetchone()
        if row is None:
            return None
        return self._rebind_from_row(row)

    def list_rebinds(
        self, document_id: str
    ) -> tuple[PerformanceFactsProfileRebind, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT rebind_id, rebind_sha256, document_id,
                       from_profile_id, to_profile_id, decided_at_utc,
                       payload_json
                FROM cad_performance_fact_rebinds
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._rebind_from_row(row) for row in rows)

    def _rebind_from_row(
        self, row: tuple
    ) -> PerformanceFactsProfileRebind:
        (
            rebind_id,
            rebind_sha256,
            document_id,
            from_profile_id,
            to_profile_id,
            decided_at_utc,
            payload_json,
        ) = row
        rebind = PerformanceFactsProfileRebind.model_validate_json(
            payload_json
        )
        if (
            rebind.rebind_id != rebind_id
            or rebind.rebind_sha256 != rebind_sha256
            or rebind.document_id != document_id
            or rebind.from_profile_id != from_profile_id
            or rebind.to_profile_id != to_profile_id
            or rebind.decided_at_utc != decided_at_utc
        ):
            raise PerformanceFactsIntegrityError(
                'performance fact rebind row disagrees with payload'
            )
        return rebind


__all__ = [
    'CadPerformanceFactsRepository',
    'PerformanceFactsConflictError',
    'PerformanceFactsIntegrityError',
]
