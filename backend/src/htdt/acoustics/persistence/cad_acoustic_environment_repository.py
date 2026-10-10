
import json
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...canonical_json import (
    canonical_json,
)
from ...clock import (
    utc_now_iso,
)
from ..domain.cad_acoustic_environment import (
    AcousticEnvironmentProfile,
    nominal_environment_profile,
)

class CadAcousticEnvironmentRepository:
    """Persist environment profiles and per-document profile selections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_environment_profiles', 'cad_environment_selections')

    @staticmethod
    def _payload(profile: AcousticEnvironmentProfile) -> str:
        return canonical_json(profile.model_dump(mode='json'))

    def save_profile(self, profile: AcousticEnvironmentProfile) -> None:
        payload = self._payload(profile)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles WHERE authority_id=?',
                (profile.authority_id,),
            ).fetchone()
            if existing is not None:
                if str(existing['payload_json']) != payload:
                    raise ValueError(
                        'acoustic environment profile id collision with different payload'
                    )
                return
            connection.execute(
                '''
                INSERT INTO cad_environment_profiles(
                    authority_id, semantic_hash_sha256, label, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    profile.authority_id,
                    profile.semantic_hash_sha256,
                    profile.label,
                    profile.created_at_utc,
                    payload,
                ),
            )

    def get_profile(self, authority_id: str) -> AcousticEnvironmentProfile | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles WHERE authority_id=?',
                (authority_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticEnvironmentProfile.model_validate(json.loads(str(row['payload_json'])))

    def list_profiles(self) -> tuple[AcousticEnvironmentProfile, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles '
                'ORDER BY created_at_utc ASC, authority_id ASC'
            ).fetchall()
        return tuple(
            AcousticEnvironmentProfile.model_validate(json.loads(str(row['payload_json'])))
            for row in rows
        )

    def ensure_default_profile(self) -> AcousticEnvironmentProfile:
        """Persist the shared nominal-assumption profile once and return it."""

        profile = nominal_environment_profile()
        self.save_profile(profile)
        return profile

    def select_profile(
        self,
        document_id: str,
        profile: AcousticEnvironmentProfile,
    ) -> None:
        if not document_id:
            raise ValueError('document_id must not be empty')
        if self.get_profile(profile.authority_id) is None:
            raise ValueError('selected environment profile is not persisted')
        updated_at = utc_now_iso()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_environment_selections(
                    document_id, authority_id, semantic_hash_sha256, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    authority_id=excluded.authority_id,
                    semantic_hash_sha256=excluded.semantic_hash_sha256,
                    updated_at_utc=excluded.updated_at_utc
                ''',
                (
                    document_id,
                    profile.authority_id,
                    profile.semantic_hash_sha256,
                    updated_at,
                ),
            )

    def clear_selection(self, document_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_environment_selections WHERE document_id=?',
                (document_id,),
            )

    def selected_profile(
        self,
        document_id: str,
    ) -> AcousticEnvironmentProfile | None:
        """The document's selected profile; fails closed on tampered rows."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT authority_id, semantic_hash_sha256 FROM cad_environment_selections '
                'WHERE document_id=?',
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        profile = self.get_profile(str(row['authority_id']))
        if profile is None or profile.semantic_hash_sha256 != str(row['semantic_hash_sha256']):
            raise ValueError('selected environment profile does not match stored identity')
        return profile

__all__ = [
    'CadAcousticEnvironmentRepository',
]
