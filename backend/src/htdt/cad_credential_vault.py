"""#890 credential vault + secret-reference authority.

As commissioning automation grows (device adapters, delegated
providers, AVR LAN transports), credentials accumulate. This module
introduces the single vault abstraction every adapter resolves secrets
through, and a sealed *reference* authority so project data carries only
non-secret metadata.

Fail-closed rules enforced here:

* project data NEVER carries secret material — :class:`CredentialReference`
  rows and :class:`CredentialLifecycleEvent` rows contain only
  references/metadata; any field that could carry a secret value is
  rejected by validator (``_SECRET_MARKERS``);
* secret material is handed out only through :class:`SecretMaterial`,
  whose ``repr``/``str`` are redacted — it must be explicitly
  ``reveal()``-ed, so a stray ``logging``/``print`` cannot leak it;
* vault state is explicit — ``unlocked``/``locked``/``unavailable``; a
  locked or missing credential raises :class:`CredentialAuthRequiredError`
  with an actionable message, never a generic failure;
* storing requires prior operator consent — a sealed
  ``consent_recorded`` lifecycle event for the document; no consent →
  :class:`CredentialConsentRequiredError`;
* rotation keeps ``credential_id`` stable — downstream evidence and
  bindings pin the logical id, so rotation never rewrites unrelated
  evidence;
* a revoked/deleted credential can never be retrieved — the vault key
  is dropped and the tombstone reference row stays for audit;
* export/diagnostic surfaces get :meth:`CredentialVaultService
  .export_manifest` — non-secret metadata only — and
  :meth:`build_redactor`, which strips known material from text before
  it reaches logs or #884 support bundles.

Platform storage:

* Windows: :class:`DpapiCredentialVault` stores one DPAPI-protected blob
  per credential (CurrentUser scope by default; ``vault_scope='machine'``
  uses ``CRYPTPROTECT_LOCAL_MACHINE`` for LocalMachine-protected blobs).
* Anywhere else / DPAPI failure: the vault reports ``unavailable`` and
  every operation fails closed — no silent plaintext fallback.
* Tests use :class:`MemoryCredentialVault`, which additionally exposes
  ``set_locked(True)`` to exercise the locked state.
"""

from __future__ import annotations

import base64
from pathlib import Path
import sys
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)


_SHA256 = r'^[0-9a-f]{64}$'

#: Markers that would indicate a field is carrying a secret *value*
#: rather than a non-secret reference/hint. Shared convention with the
#: #726/#879 ``credential_ref`` guard.
_SECRET_MARKERS = (
    'pass',
    'token',
    'secret',
    'material',
    'credential_value',
    'private_key',
)


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal_id(
    model_cls: type[BaseModel], prefix: str, payload: Mapping[str, Any],
) -> tuple[str, str]:
    probe = model_cls.model_construct(
        **canonicalize_payload(model_cls, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return _semantic_id(prefix, digest), digest


def _ref(kind: str, record_id: str, sha: str) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=record_id, ref_sha256=sha)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Errors


class CredentialVaultError(Exception):
    """Base for credential-vault failures (fail-closed surfaces)."""


class CredentialAuthRequiredError(CredentialVaultError):
    """The credential cannot be retrieved right now — locked, unavailable
    vault or missing credential. Message is always actionable."""


class CredentialConsentRequiredError(CredentialVaultError):
    """A store was attempted before the operator recorded consent."""


class CredentialReferenceError(CredentialVaultError):
    """A reference lookup/transition failed the evidence gates."""


class CredentialStorageError(CredentialVaultError):
    """The platform vault backend failed a write/delete operation."""


# ---------------------------------------------------------------------------
# Vocabulary

CredentialType = Literal[
    'password',
    'api_token',
    'certificate',
    'device_key',
    'pairing_secret',
    'account',
]

CredentialScopeKind = Literal['provider', 'device', 'service']

#: Who the platform protects the secret for. ``user`` = current user
#: profile (DPAPI CurrentUser); ``machine`` = any account on this host
#: (DPAPI LocalMachine). Machine-scoped material does not travel with the
#: user profile and must not be assumed readable after a re-image.
VaultScope = Literal['user', 'machine']

VaultState = Literal['unlocked', 'locked', 'unavailable']

CredentialReferenceState = Literal['active', 'revoked', 'deleted']

CredentialEventKind = Literal[
    'consent_recorded',
    'stored',
    'rotated',
    'revoked',
    'deleted',
    'used',
    'retrieve_denied',
]


# ---------------------------------------------------------------------------
# Secret material — the only object that ever carries the value.


class SecretMaterial:
    """Opaque holder for secret bytes.

    ``repr``/``str`` are redacted by construction; the only way out is
    :meth:`reveal` / :meth:`reveal_bytes` or :meth:`use` (which scopes the
    exposure to a callable). ``fingerprint`` is a non-secret sha256 that
    diagnostics may compare/log safely.
    """

    __slots__ = ('_data',)

    def __init__(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode('utf-8')
        if not data:
            raise CredentialVaultError('secret material cannot be empty')
        object.__setattr__(self, '_data', bytes(data))

    def reveal(self) -> str:
        return self._data.decode('utf-8')

    def reveal_bytes(self) -> bytes:
        return self._data

    def use(self, fn: Callable[[str], Any]) -> Any:
        return fn(self.reveal())

    def fingerprint(self) -> str:
        return _hash({'material_sha256_preimage': self._data.hex()})

    def __repr__(self) -> str:  # noqa: D105
        return '«credential material — redacted»'

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, SecretMaterial) and other._data == self._data
        )

    __hash__ = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Vault backends


class CredentialVault:
    """Platform secure-store abstraction.

    Keys are opaque vault keys (``htdt-cred/<uuid>``); only
    :class:`SecretMaterial` crosses the boundary.
    """

    def state(self) -> VaultState:
        raise NotImplementedError

    def store(self, key: str, material: SecretMaterial) -> None:
        raise NotImplementedError

    def retrieve(self, key: str) -> SecretMaterial:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def contains(self, key: str) -> bool:
        raise NotImplementedError

    def _require_unlocked(self) -> None:
        state = self.state()
        if state == 'unavailable':
            raise CredentialAuthRequiredError(
                'credential vault is unavailable on this platform — '
                'secrets cannot be stored or retrieved here'
            )
        if state == 'locked':
            raise CredentialAuthRequiredError(
                'credential vault is locked — unlock the platform '
                'credential store and retry'
            )


class MemoryCredentialVault(CredentialVault):
    """In-memory vault for tests and off-platform builds."""

    def __init__(self, *, locked: bool = False) -> None:
        self._store: dict[str, bytes] = {}
        self._locked = locked

    def set_locked(self, locked: bool) -> None:
        self._locked = locked

    def state(self) -> VaultState:
        return 'locked' if self._locked else 'unlocked'

    def store(self, key: str, material: SecretMaterial) -> None:
        self._require_unlocked()
        self._store[key] = material.reveal_bytes()

    def retrieve(self, key: str) -> SecretMaterial:
        self._require_unlocked()
        try:
            return SecretMaterial(self._store[key])
        except KeyError:
            raise CredentialAuthRequiredError(
                f'credential {key!r} not found in vault — store it '
                'via the credential vault before use'
            ) from None

    def delete(self, key: str) -> None:
        self._require_unlocked()
        self._store.pop(key, None)

    def contains(self, key: str) -> bool:
        return key in self._store


class UnavailableCredentialVault(CredentialVault):
    """Vault backend for platforms with no secure facility — every
    operation fails closed."""

    def state(self) -> VaultState:
        return 'unavailable'

    def store(self, key: str, material: SecretMaterial) -> None:
        self._require_unlocked()

    def retrieve(self, key: str) -> SecretMaterial:
        self._require_unlocked()
        raise AssertionError('unreachable')

    def delete(self, key: str) -> None:
        self._require_unlocked()

    def contains(self, key: str) -> bool:
        return False


class _DpapiBlob(__import__('ctypes').Structure):  # noqa: N801
    import ctypes as _ct
    import ctypes.wintypes as _wt

    _fields_ = [('cbData', _wt.DWORD), ('pbData', _ct.c_void_p)]


class DpapiCredentialVault(CredentialVault):
    """Windows DPAPI-backed vault: one protected blob per credential.

    ``user`` scope → ``CryptProtectData`` (CurrentUser); ``machine``
    scope adds ``CRYPTPROTECT_LOCAL_MACHINE``. Blobs live under
    ``root/<key-sha>.bin``; the key name is never the secret. Any DPAPI
    failure reports ``unavailable`` — there is no plaintext fallback.
    """

    _CRYPTPROTECT_LOCAL_MACHINE = 0x4

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._usable: bool | None = None

    # -- ctypes plumbing -------------------------------------------------

    def _crypt32(self):
        import ctypes

        return ctypes.windll.crypt32, ctypes.windll.kernel32

    def _protect(self, data: bytes, machine: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        crypt32, kernel32 = self._crypt32()
        blob_in = _DpapiBlob()
        blob_in.cbData = len(data)
        blob_in.pbData = ctypes.cast(
            ctypes.create_string_buffer(data, len(data)),
            ctypes.c_void_p,
        )
        blob_out = _DpapiBlob()
        flags = self._CRYPTPROTECT_LOCAL_MACHINE if machine else 0
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            flags, ctypes.byref(blob_out),
        ):
            raise CredentialStorageError('CryptProtectData failed')
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(wintypes.HLOCAL(blob_out.pbData))

    def _unprotect(self, data: bytes) -> bytes:
        import ctypes
        from ctypes import wintypes

        crypt32, kernel32 = self._crypt32()
        blob_in = _DpapiBlob()
        blob_in.cbData = len(data)
        blob_in.pbData = ctypes.cast(
            ctypes.create_string_buffer(data, len(data)),
            ctypes.c_void_p,
        )
        blob_out = _DpapiBlob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            0, ctypes.byref(blob_out),
        ):
            raise CredentialStorageError('CryptUnprotectData failed')
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(wintypes.HLOCAL(blob_out.pbData))

    # -- vault API --------------------------------------------------------

    def _path(self, key: str, machine: bool = False) -> Path:
        digest = _hash({'vault_key': key, 'machine': machine})
        scope_dir = 'machine' if machine else 'user'
        return self._root / scope_dir / f'{digest}.bin'

    def state(self) -> VaultState:
        if sys.platform != 'win32':
            return 'unavailable'
        if self._usable is None:
            try:
                self._unprotect(self._protect(b'htdt-probe', False))
                self._usable = True
            except Exception:  # error-boundary: DPAPI backend probe
                self._usable = False
        return 'unlocked' if self._usable else 'unavailable'

    def store(
        self, key: str, material: SecretMaterial, *, machine: bool = False,
    ) -> None:
        self._require_unlocked()
        path = self._path(key, machine)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._protect(material.reveal_bytes(), machine))

    def retrieve(self, key: str, *, machine: bool = False) -> SecretMaterial:
        self._require_unlocked()
        path = self._path(key, machine)
        if not path.exists():
            raise CredentialAuthRequiredError(
                f'credential {key!r} not found in vault — store it '
                'via the credential vault before use'
            )
        return SecretMaterial(self._unprotect(path.read_bytes()))

    def delete(self, key: str, *, machine: bool = False) -> None:
        self._require_unlocked()
        self._path(key, machine).unlink(missing_ok=True)

    def contains(self, key: str, *, machine: bool = False) -> bool:
        return self._path(key, machine).exists()


def platform_vault(root: Path | None = None) -> CredentialVault:
    """Best available platform vault — DPAPI on Windows, else the
    unavailable stub (never a plaintext fallback)."""

    if sys.platform == 'win32':
        import os

        if root is None:
            base = os.environ.get('LOCALAPPDATA') or str(
                Path.home() / 'AppData' / 'Local'
            )
            root = Path(base) / 'HTDT' / 'credential-vault'
        return DpapiCredentialVault(root)
    return UnavailableCredentialVault()


# ---------------------------------------------------------------------------
# Sealed records


def _reject_secret_fields(
    model: BaseModel, field_names: tuple[str, ...],
) -> None:
    for name in field_names:
        value = getattr(model, name, None)
        if isinstance(value, str) and any(
            marker in value.lower() for marker in _SECRET_MARKERS
        ):
            raise ValueError(
                f'{name} is a non-secret reference/hint — secret values '
                'never enter project data'
            )


class CredentialReference(BaseModel):
    """One sealed reference row for a stored credential.

    Rows are append-only per ``credential_id``: rotation stores a new row
    with ``version`` bumped; revoke/delete store a terminal row with the
    matching ``state``. ``credential_id`` is stable across the whole
    lifecycle so downstream evidence (bindings, deployments) pins the
    logical credential, not a particular secret version.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    reference_id: str = Field(min_length=1)
    reference_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    credential_id: str = Field(min_length=1)
    scope_kind: CredentialScopeKind
    scope_ref: str = Field(min_length=1)
    credential_type: CredentialType
    vault_scope: VaultScope
    vault_key: str = Field(min_length=1)
    state: CredentialReferenceState
    version: int = Field(ge=1)
    identity_hint: str | None = None
    supersedes_ref: AuthorityRef | None = None
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_reference(self) -> 'CredentialReference':
        _reject_secret_fields(self, ('identity_hint',))
        if self.vault_key.startswith('htdt-cred/') is False:
            raise ValueError('vault_key must be an opaque htdt-cred/ key')
        if self.state != 'active' and self.version < 1:
            raise ValueError('non-active state requires a prior version')
        if (
            self.version > 1
            and self.supersedes_ref is None
        ):
            raise ValueError(
                'version > 1 must supersede the previous reference row'
            )
        if self.reference_sha256 != _hash(self.identity_payload()):
            raise ValueError('CredentialReference hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'reference_id', 'reference_sha256'},
        )


class CredentialLifecycleEvent(BaseModel):
    """One sealed lifecycle event — consent, store, rotate, revoke,
    delete, use. ``details`` is metadata only; secret-shaped keys/values
    are rejected."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    event_id: str = Field(min_length=1)
    event_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    credential_id: str = Field(min_length=1)
    event_kind: CredentialEventKind
    actor: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    details: Mapping[str, str] = ()

    @model_validator(mode='after')
    def valid_event(self) -> 'CredentialLifecycleEvent':
        for key, value in (self.details or {}).items():
            lowered = key.lower()
            if (
                any(m in lowered for m in ('secret', 'password', 'token', 'material'))
                or lowered in ('key', 'value')
            ):
                raise ValueError(
                    f'details key {key!r} must not carry secret material'
                )
            if isinstance(value, str) and len(value) > 512:
                raise ValueError('details values are metadata, not payloads')
        if self.event_sha256 != _hash(self.identity_payload()):
            raise ValueError('CredentialLifecycleEvent hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_id', 'event_sha256'},
        )


# ---------------------------------------------------------------------------
# Redaction — strips known material before text reaches logs/bundles.


class SecretRedactor:
    """Replaces every registered secret value in text with
    ``«redacted:<credential_id>»``. Register each material at retrieval
    time so transport transcripts, diagnostics and #884 support bundles
    can be scrubbed before leaving the process."""

    def __init__(self) -> None:
        self._pairs: list[tuple[str, str]] = []

    def register(self, credential_id: str, material: SecretMaterial) -> None:
        value = material.reveal()
        if value:
            self._pairs.append((value, f'«redacted:{credential_id}»'))

    def redact(self, text: str) -> str:
        for raw, replacement in self._pairs:
            text = text.replace(raw, replacement)
        return text


# ---------------------------------------------------------------------------
# Service — one abstraction every adapter resolves through.


def _new_vault_key() -> str:
    return f'htdt-cred/{uuid4().hex}'


class CredentialVaultService:
    """Orchestrates sealed reference/event records plus the platform
    vault. Constructed with a ``CadCredentialVaultRepository`` and a
    :class:`CredentialVault`."""

    def __init__(self, repository: Any, vault: CredentialVault) -> None:
        self._repo = repository
        self._vault = vault

    # -- consent ---------------------------------------------------------

    def record_consent(
        self, document_id: str, actor: str, *, note: str | None = None,
    ) -> CredentialLifecycleEvent:
        """Operator consent to store credentials for this document —
        required before the first ``store_credential``."""

        details = {'note': note} if note else {}
        payload = {
            'document_id': document_id,
            'credential_id': '*',
            'event_kind': 'consent_recorded',
            'actor': actor,
            'recorded_at_utc': _utc_now(),
            'details': details,
        }
        event_id, event_sha = _seal_id(
            CredentialLifecycleEvent, 'clev', payload,
        )
        event = CredentialLifecycleEvent(
            event_id=event_id, event_sha256=event_sha, **payload,
        )
        self._repo.append_event(event)
        return event

    def _require_consent(self, document_id: str) -> None:
        if not self._repo.has_consent(document_id):
            raise CredentialConsentRequiredError(
                'operator consent must be recorded before storing '
                'credentials for this project'
            )

    # -- lifecycle ---------------------------------------------------------

    def store_credential(
        self,
        document_id: str,
        *,
        scope_kind: CredentialScopeKind,
        scope_ref: str,
        credential_type: CredentialType,
        material: SecretMaterial | str,
        actor: str,
        vault_scope: VaultScope = 'user',
        identity_hint: str | None = None,
    ) -> CredentialReference:
        """Store new secret material + a sealed reference.

        Requires prior consent. On a locked/unavailable vault the call
        fails closed before any reference row is written."""

        self._require_consent(document_id)
        if not isinstance(material, SecretMaterial):
            material = SecretMaterial(material)
        vault_key = _new_vault_key()
        credential_id = _semantic_id(
            'crid',
            _hash({
                'document_id': document_id,
                'scope_kind': scope_kind,
                'scope_ref': scope_ref,
                'credential_type': credential_type,
                'vault_key': vault_key,
            }),
        )
        # Write material FIRST so a failed store leaves no dangling ref.
        try:
            self._store_material(vault_key, material, vault_scope)
        except CredentialVaultError:
            raise
        except Exception as exc:  # error-boundary: vault backend write
            raise CredentialStorageError(
                f'vault store failed for {vault_key!r}'
            ) from exc
        reference = self._write_reference(
            document_id=document_id,
            credential_id=credential_id,
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            credential_type=credential_type,
            vault_scope=vault_scope,
            vault_key=vault_key,
            state='active',
            version=1,
            identity_hint=identity_hint,
            supersedes_ref=None,
        )
        self._append_event(
            document_id, credential_id, 'stored', actor,
            {'scope_kind': scope_kind, 'scope_ref': scope_ref},
        )
        return reference

    def _store_material(
        self,
        vault_key: str,
        material: SecretMaterial,
        vault_scope: VaultScope,
    ) -> None:
        if vault_scope == 'user':
            self._vault.store(vault_key, material)
            return
        if isinstance(self._vault, DpapiCredentialVault):
            self._vault.store(vault_key, material, machine=True)
            return
        raise CredentialStorageError(
            'vault backend does not support machine scope'
        )

    def _retrieve_material(
        self, vault_key: str, vault_scope: VaultScope,
    ) -> SecretMaterial:
        if vault_scope == 'user':
            return self._vault.retrieve(vault_key)
        if isinstance(self._vault, DpapiCredentialVault):
            return self._vault.retrieve(vault_key, machine=True)
        raise CredentialAuthRequiredError(
            'machine-scope credential cannot be read by this vault '
            'backend — restore DPAPI vault access'
        )

    def _delete_material(
        self, vault_key: str, vault_scope: VaultScope,
    ) -> None:
        if vault_scope == 'user':
            self._vault.delete(vault_key)
            return
        if isinstance(self._vault, DpapiCredentialVault):
            self._vault.delete(vault_key, machine=True)
            return
        self._vault.delete(vault_key)

    def _write_reference(self, **fields: Any) -> CredentialReference:
        fields.setdefault('created_at_utc', _utc_now())
        reference_id, sha = _seal_id(
            CredentialReference, 'cred', fields,
        )
        reference = CredentialReference(
            reference_id=reference_id,
            reference_sha256=sha,
            **fields,
        )
        self._repo.append_reference(reference)
        return reference

    def _append_event(
        self,
        document_id: str,
        credential_id: str,
        kind: CredentialEventKind,
        actor: str,
        details: Mapping[str, str] | None = None,
    ) -> CredentialLifecycleEvent:
        payload = {
            'document_id': document_id,
            'credential_id': credential_id,
            'event_kind': kind,
            'actor': actor,
            'recorded_at_utc': _utc_now(),
            'details': dict(details or {}),
        }
        event_id, sha = _seal_id(
            CredentialLifecycleEvent, 'clev', payload,
        )
        event = CredentialLifecycleEvent(
            event_id=event_id, event_sha256=sha, **payload,
        )
        self._repo.append_event(event)
        return event

    # -- retrieval ---------------------------------------------------------

    def _current(self, credential_id: str) -> CredentialReference:
        reference = self._repo.current_reference(credential_id)
        if reference is None:
            raise CredentialAuthRequiredError(
                f'credential {credential_id!r} is not stored — register '
                'it in the credential vault first'
            )
        return reference

    def retrieve(
        self, credential_id: str, *, actor: str = 'system',
    ) -> SecretMaterial:
        reference = self._current(credential_id)
        if reference.state == 'deleted':
            self._append_event(
                reference.document_id, credential_id,
                'retrieve_denied', actor, {'reason': 'deleted'},
            )
            raise CredentialAuthRequiredError(
                f'credential {credential_id!r} was deleted — store a '
                'new credential and re-bind the adapter'
            )
        if reference.state == 'revoked':
            self._append_event(
                reference.document_id, credential_id,
                'retrieve_denied', actor, {'reason': 'revoked'},
            )
            raise CredentialAuthRequiredError(
                f'credential {credential_id!r} is revoked — re-enable '
                'or replace it before use'
            )
        try:
            material = self._retrieve_material(
                reference.vault_key, reference.vault_scope,
            )
        except CredentialVaultError:
            raise
        except Exception as exc:  # error-boundary: vault backend read
            raise CredentialAuthRequiredError(
                'credential vault read failed — check the platform '
                'credential store is available'
            ) from exc
        self._append_event(
            reference.document_id, credential_id, 'used', actor, {},
        )
        return material

    def resolver(
        self, *, actor: str = 'adapter',
    ) -> Callable[[str], SecretMaterial]:
        """``credential_resolver`` callable matching the #726/#879
        transport convention: the caller passes a credential name —
        ``credential_id``, ``reference_id`` or ``vault_key`` — never a
        secret value."""

        def _resolve(credential_ref: str) -> SecretMaterial:
            reference = self._repo.find_reference(credential_ref)
            if reference is None:
                raise CredentialAuthRequiredError(
                    f'credential reference {credential_ref!r} does not '
                    'resolve — re-create it in the credential vault'
                )
            return self.retrieve(
                reference.credential_id, actor=actor,
            )

        return _resolve

    # -- rotation / revocation / deletion ------------------------------------

    def rotate(
        self,
        credential_id: str,
        *,
        material: SecretMaterial | str,
        actor: str,
    ) -> CredentialReference:
        """Replace the secret under a NEW reference row — ``credential_id``
        stays stable, so pinned evidence and bindings keep resolving."""

        reference = self._current(credential_id)
        if reference.state != 'active':
            raise CredentialReferenceError(
                f'cannot rotate a {reference.state} credential'
            )
        if not isinstance(material, SecretMaterial):
            material = SecretMaterial(material)
        self._store_material(
            reference.vault_key, material, reference.vault_scope,
        )
        rotated = self._write_reference(
            document_id=reference.document_id,
            credential_id=credential_id,
            scope_kind=reference.scope_kind,
            scope_ref=reference.scope_ref,
            credential_type=reference.credential_type,
            vault_scope=reference.vault_scope,
            vault_key=reference.vault_key,
            state='active',
            version=reference.version + 1,
            identity_hint=reference.identity_hint,
            supersedes_ref=_ref(
                'credential_reference',
                reference.reference_id,
                reference.reference_sha256,
            ),
        )
        self._append_event(
            reference.document_id, credential_id, 'rotated', actor,
            {'version': str(rotated.version)},
        )
        return rotated

    def revoke(
        self, credential_id: str, *, actor: str,
    ) -> CredentialReference:
        reference = self._current(credential_id)
        if reference.state != 'active':
            raise CredentialReferenceError(
                f'cannot revoke a {reference.state} credential'
            )
        tombstone = self._write_reference(
            document_id=reference.document_id,
            credential_id=credential_id,
            scope_kind=reference.scope_kind,
            scope_ref=reference.scope_ref,
            credential_type=reference.credential_type,
            vault_scope=reference.vault_scope,
            vault_key=reference.vault_key,
            state='revoked',
            version=reference.version + 1,
            identity_hint=reference.identity_hint,
            supersedes_ref=_ref(
                'credential_reference',
                reference.reference_id,
                reference.reference_sha256,
            ),
        )
        self._append_event(
            reference.document_id, credential_id, 'revoked', actor, {},
        )
        return tombstone

    def delete(
        self, credential_id: str, *, actor: str,
    ) -> CredentialReference:
        reference = self._current(credential_id)
        if reference.state == 'deleted':
            raise CredentialReferenceError('credential already deleted')
        self._delete_material(reference.vault_key, reference.vault_scope)
        tombstone = self._write_reference(
            document_id=reference.document_id,
            credential_id=credential_id,
            scope_kind=reference.scope_kind,
            scope_ref=reference.scope_ref,
            credential_type=reference.credential_type,
            vault_scope=reference.vault_scope,
            vault_key=reference.vault_key,
            state='deleted',
            version=reference.version + 1,
            identity_hint=reference.identity_hint,
            supersedes_ref=_ref(
                'credential_reference',
                reference.reference_id,
                reference.reference_sha256,
            ),
        )
        self._append_event(
            reference.document_id, credential_id, 'deleted', actor, {},
        )
        return tombstone

    # -- non-secret surfaces ---------------------------------------------------

    def vault_state(self) -> VaultState:
        """Current platform-vault state — drives honest operator UI
        (locked/unavailable disable material operations)."""

        return self._vault.state()

    def has_consent(self, document_id: str) -> bool:
        """Whether operator consent is on record for the document."""

        return self._repo.has_consent(document_id)

    def list_references(
        self, document_id: str,
    ) -> tuple[CredentialReference, ...]:
        """Every sealed reference row — metadata only, never material."""

        return self._repo.list_references(document_id)

    def list_events(
        self,
        document_id: str,
        *,
        credential_id: str | None = None,
    ) -> tuple[CredentialLifecycleEvent, ...]:
        """Sealed lifecycle events — the operator audit surface."""

        return self._repo.list_events(
            document_id, credential_id=credential_id,
        )

    def export_manifest(
        self, document_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Non-secret metadata for project export/diagnostics — proves
        the export carries references, never material."""

        out = []
        for reference in self._repo.list_references(document_id):
            out.append({
                'credential_id': reference.credential_id,
                'scope_kind': reference.scope_kind,
                'scope_ref': reference.scope_ref,
                'credential_type': reference.credential_type,
                'vault_scope': reference.vault_scope,
                'state': reference.state,
                'version': reference.version,
                'identity_hint': reference.identity_hint,
                'created_at_utc': reference.created_at_utc,
            })
        return tuple(out)

    def build_redactor(self, document_id: str) -> SecretRedactor:
        """Redactor pre-loaded with every retrievable credential for the
        document — use before diagnostics/support-bundle surfaces."""

        redactor = SecretRedactor()
        for reference in self._repo.list_references(document_id):
            if reference.state != 'active':
                continue
            try:
                material = self.retrieve(
                    reference.credential_id, actor='redactor',
                )
            except CredentialVaultError:
                continue  # locked/missing — nothing to scrub
            redactor.register(reference.credential_id, material)
        return redactor
