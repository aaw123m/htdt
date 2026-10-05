"""Append-only persistence for the stimulus/test-asset registry (#608).

Three authorities live here:

* ``cad_stimulus_assets`` — immutable registry entries keyed by
  ``stimulus_id`` with the semantic hash carried as an indexed column.
  Re-saving an identical row is a no-op; a divergent hash for the same id
  is a conflict — a registered stimulus can never be silently revised.
* ``cad_stimulus_pins`` — measurement/dataset → stimulus bindings.
* ``cad_stimulus_eligibility`` — sealed procedure-eligibility verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_schema import require_native_tables, connect_sqlite
from .cad_stimulus_registry import (
    StimulusAssetEntry,
    StimulusEligibilityRecord,
    StimulusMeasurementPin,
)
from .cad_repository import SceneRepository
from .clock import utc_now_iso as _utc_now


class StimulusRegistryConflictError(ValueError):
    """A registry save violated append-only identity rules."""


class StimulusRegistryIntegrityError(ValueError):
    """A stored row disagreed with its payload."""


class CadStimulusRegistryRepository:
    """Native storage for stimulus registry entries, pins and verdicts."""

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
                'cad_stimulus_assets',
                'cad_stimulus_pins',
                'cad_stimulus_eligibility',
            )

    # ------------------------------------------------------------------
    # Registry entries

    def save_asset(self, entry: StimulusAssetEntry) -> None:
        existing = self.get_asset(entry.stimulus_id)
        if existing is not None:
            if existing.stimulus_sha256 == entry.stimulus_sha256:
                return
            raise StimulusRegistryConflictError(
                'stimulus registry entries are append-only — a stimulus_id '
                'with different content requires a new stimulus_id'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_stimulus_assets (
                    stimulus_id, stimulus_sha256, document_id, origin_class,
                    subtype, content_sha256, registered_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.stimulus_id,
                    entry.stimulus_sha256,
                    entry.document_id,
                    entry.origin_class,
                    entry.subtype,
                    entry.content_sha256,
                    entry.registered_at_utc,
                    entry.model_dump_json(),
                ),
            )

    def get_asset(self, stimulus_id: str) -> StimulusAssetEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT stimulus_id, stimulus_sha256, document_id,
                       origin_class, subtype, payload_json
                FROM cad_stimulus_assets
                WHERE stimulus_id=?
                """,
                (stimulus_id,),
            ).fetchone()
        if row is None:
            return None
        return self._asset_from_row(row)

    def get_asset_by_hash(
        self, stimulus_sha256: str
    ) -> StimulusAssetEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT stimulus_id, stimulus_sha256, document_id,
                       origin_class, subtype, payload_json
                FROM cad_stimulus_assets
                WHERE stimulus_sha256=?
                """,
                (stimulus_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._asset_from_row(row)

    def lookup_by_content_hash(
        self, content_sha256: str
    ) -> tuple[StimulusAssetEntry, ...]:
        """Registry entries whose pinned bytes match this content hash —
        substitution detection runs on content, not labels."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT stimulus_id, stimulus_sha256, document_id,
                       origin_class, subtype, payload_json
                FROM cad_stimulus_assets
                WHERE content_sha256=?
                ORDER BY registered_at_utc, stimulus_id
                """,
                (content_sha256,),
            ).fetchall()
        return tuple(self._asset_from_row(row) for row in rows)

    def list_assets(
        self, document_id: str
    ) -> tuple[StimulusAssetEntry, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT stimulus_id, stimulus_sha256, document_id,
                       origin_class, subtype, payload_json
                FROM cad_stimulus_assets
                WHERE document_id=?
                ORDER BY registered_at_utc, stimulus_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._asset_from_row(row) for row in rows)

    def _asset_from_row(self, row: sqlite3.Row) -> StimulusAssetEntry:
        entry = StimulusAssetEntry.model_validate_json(row['payload_json'])
        if (
            entry.stimulus_id != row['stimulus_id']
            or entry.stimulus_sha256 != row['stimulus_sha256']
            or entry.document_id != row['document_id']
            or entry.origin_class != row['origin_class']
            or entry.subtype != row['subtype']
        ):
            raise StimulusRegistryIntegrityError(
                'stimulus asset row disagrees with its payload'
            )
        return entry

    # ------------------------------------------------------------------
    # Measurement pins

    def save_pin(self, pin: StimulusMeasurementPin) -> None:
        existing = self.get_pin(pin.pin_id)
        if existing is not None:
            if existing.pin_sha256 == pin.pin_sha256:
                return
            raise StimulusRegistryConflictError(
                'stimulus pins are append-only'
            )
        asset = self.get_asset(pin.stimulus_id)
        if asset is None:
            raise StimulusRegistryIntegrityError(
                'a pin must reference a persisted registry entry — pinning '
                'an unregistered stimulus is not allowed'
            )
        if asset.stimulus_sha256 != pin.stimulus_sha256:
            raise StimulusRegistryIntegrityError(
                'pin stimulus hash does not match the registered entry'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_stimulus_pins (
                    pin_id, pin_sha256, document_id, measurement_ref,
                    stimulus_id, stimulus_sha256, pinned_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pin.pin_id,
                    pin.pin_sha256,
                    pin.document_id,
                    pin.measurement_ref,
                    pin.stimulus_id,
                    pin.stimulus_sha256,
                    pin.pinned_at_utc,
                    pin.model_dump_json(),
                ),
            )

    def get_pin(self, pin_id: str) -> StimulusMeasurementPin | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT pin_id, pin_sha256, document_id, measurement_ref,
                       stimulus_id, stimulus_sha256, pinned_at_utc,
                       payload_json
                FROM cad_stimulus_pins
                WHERE pin_id=?
                """,
                (pin_id,),
            ).fetchone()
        if row is None:
            return None
        return self._pin_from_row(row)

    def pins_for_measurement(
        self, measurement_ref: str
    ) -> tuple[StimulusMeasurementPin, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT pin_id, pin_sha256, document_id, measurement_ref,
                       stimulus_id, stimulus_sha256, pinned_at_utc,
                       payload_json
                FROM cad_stimulus_pins
                WHERE measurement_ref=?
                ORDER BY pinned_at_utc, pin_id
                """,
                (measurement_ref,),
            ).fetchall()
        return tuple(self._pin_from_row(row) for row in rows)

    def list_pins(
        self, document_id: str
    ) -> tuple[StimulusMeasurementPin, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT pin_id, pin_sha256, document_id, measurement_ref,
                       stimulus_id, stimulus_sha256, pinned_at_utc,
                       payload_json
                FROM cad_stimulus_pins
                WHERE document_id=?
                ORDER BY pinned_at_utc, pin_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._pin_from_row(row) for row in rows)

    def _pin_from_row(self, row: sqlite3.Row) -> StimulusMeasurementPin:
        pin = StimulusMeasurementPin.model_validate_json(row['payload_json'])
        if (
            pin.pin_id != row['pin_id']
            or pin.pin_sha256 != row['pin_sha256']
            or pin.document_id != row['document_id']
            or pin.measurement_ref != row['measurement_ref']
            or pin.stimulus_id != row['stimulus_id']
            or pin.stimulus_sha256 != row['stimulus_sha256']
            or pin.pinned_at_utc != row['pinned_at_utc']
        ):
            raise StimulusRegistryIntegrityError(
                'stimulus pin row disagrees with its payload'
            )
        return pin

    # ------------------------------------------------------------------
    # Eligibility verdicts

    def save_eligibility(self, record: StimulusEligibilityRecord) -> None:
        existing = self.get_eligibility(record.eligibility_id)
        if existing is not None:
            if existing.eligibility_sha256 == record.eligibility_sha256:
                return
            raise StimulusRegistryConflictError(
                'eligibility verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_stimulus_eligibility (
                    eligibility_id, eligibility_sha256, document_id,
                    procedure_id, stimulus_id, stimulus_sha256,
                    verdict, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.eligibility_id,
                    record.eligibility_sha256,
                    record.document_id,
                    record.procedure_id,
                    record.stimulus_id,
                    record.stimulus_sha256,
                    record.verdict,
                    record.evaluated_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_eligibility(
        self, eligibility_id: str
    ) -> StimulusEligibilityRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT eligibility_id, eligibility_sha256, document_id,
                       procedure_id, stimulus_id, stimulus_sha256,
                       verdict, payload_json
                FROM cad_stimulus_eligibility
                WHERE eligibility_id=?
                """,
                (eligibility_id,),
            ).fetchone()
        if row is None:
            return None
        return self._eligibility_from_row(row)

    def list_eligibility(
        self, document_id: str
    ) -> tuple[StimulusEligibilityRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT eligibility_id, eligibility_sha256, document_id,
                       procedure_id, stimulus_id, stimulus_sha256,
                       verdict, payload_json
                FROM cad_stimulus_eligibility
                WHERE document_id=?
                ORDER BY evaluated_at_utc, eligibility_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._eligibility_from_row(row) for row in rows)

    def _eligibility_from_row(
        self, row: sqlite3.Row
    ) -> StimulusEligibilityRecord:
        record = StimulusEligibilityRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.eligibility_id != row['eligibility_id']
            or record.eligibility_sha256 != row['eligibility_sha256']
            or record.document_id != row['document_id']
            or record.procedure_id != row['procedure_id']
            or record.stimulus_id != row['stimulus_id']
            or record.stimulus_sha256 != row['stimulus_sha256']
            or record.verdict != row['verdict']
        ):
            raise StimulusRegistryIntegrityError(
                'stimulus eligibility row disagrees with its payload'
            )
        return record


__all__ = [
    'CadStimulusRegistryRepository',
    'StimulusRegistryConflictError',
    'StimulusRegistryIntegrityError',
]
