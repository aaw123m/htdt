"""#1059 — Acoustic Index material-data connector (read-through).

Bridges the Acoustic Index Read API (``https://acousticindex.com/api``) into
HTDT material evidence WITHOUT mirroring third-party content: a lookup is a
search → candidate → user-selects → immutable snapshot → import pipeline.
What enters a project is the snapshot — a verbatim capture of the API
response, hash-pinned, carrying the upstream evidence tier exactly as the
service reported it — never a transcribed catalog row.

Contract carried by this module:

- the **public, unauthenticated** API surface is used first
  (``GET /api/public/search``, ``GET /api/public/compare-products``); no
  credential is stored anywhere in project data;
- build-up identity is mandatory: thickness / air-gap / backing / mounting
  are what distinguish two measurements of "the same" material, so a
  snapshot without build-up metadata is flagged, not silently imported;
- metric families are never conflated: ``alpha_s`` (ISO 354 / ISO 11654
  absorption classes), ``alpha_p`` (ISO 354 practical), ``alpha_w`` /
  ``alpha_w_min`` (ISO 11654 weighted), and the *derived* NRC/SAA block are
  kept as separate fields. ``alpha_w`` is never recomputed from band data;
  NRC is never equated with αw;
- original evidence tier is preserved verbatim — ``data_source`` /
  ``measuring_option``/``source_kind`` pass through untouched; a
  manufacturer row never becomes "independent";
- import is fail-closed: non-monotonic frequency axes, out-of-bounds
  coefficients, and missing bands inside a stated series raise
  ``AcousticIndexImportError`` — nothing is silently repaired;
- the cache is bounded (``max_entries``) and *rebinds* rather than
  rewrites: a changed upstream response produces a new snapshot that
  supersedes the old one; existing project bindings keep their original
  snapshot id;
- offline-safe: the connector never requires network — a caller injects a
  fetcher; when offline it returns cached snapshots or reports the lookup
  as unavailable.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ...canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


ACOUSTIC_INDEX_AUTHORITY_VERSION = 'acoustic-index-1'
ACOUSTIC_INDEX_API_BASE = 'https://acousticindex.com/api'
ACOUSTIC_INDEX_PUBLIC_SEARCH = f'{ACOUSTIC_INDEX_API_BASE}/public/search'
ACOUSTIC_INDEX_PUBLIC_COMPARE = (
    f'{ACOUSTIC_INDEX_API_BASE}/public/compare-products'
)

# ISO 354 coefficients may legitimately exceed 1.0 (edge diffraction);
# 2.0 is the sanity bound used to reject corrupt data, not physics.
_ABSORPTION_SANITY_MAX = 2.0






class AcousticIndexError(RuntimeError):
    pass


class AcousticIndexImportError(AcousticIndexError):
    """Raised when a snapshot fails import-time quality checks."""


class AcousticIndexOfflineError(AcousticIndexError):
    """Raised when a lookup needs the network and none is available."""


# ---------------------------------------------------------------------------
# Search results — discovery only, not material evidence
# ---------------------------------------------------------------------------


class AcousticIndexCandidate(BaseModel):
    """One search hit — a pointer the user may select, never evidence."""

    model_config = ConfigDict(frozen=True)

    product_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    commercial_name: str | None = None
    manufacturer_name: str | None = None
    material_category: str | None = None
    data_source: str | None = None
    alpha_w: float | None = None
    alpha_w_min: float | None = None


class AcousticIndexSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['acoustic-index-1'] = (
        ACOUSTIC_INDEX_AUTHORITY_VERSION
    )
    query: str = ''
    retrieved_utc: str = Field(min_length=1)
    live_fetch: bool
    candidates: tuple[AcousticIndexCandidate, ...]


# ---------------------------------------------------------------------------
# Snapshot — the immutable import payload
# ---------------------------------------------------------------------------


class AbsorptionSeries(BaseModel):
    """One named coefficient series kept on its own semantic footing."""

    model_config = ConfigDict(frozen=True)

    frequencies_hz: tuple[float, ...]
    coefficients: tuple[float, ...]

    @model_validator(mode='after')
    def _check(self) -> 'AbsorptionSeries':
        if len(self.frequencies_hz) != len(self.coefficients):
            raise ValueError('frequency/coefficient length mismatch')
        return self

    def is_monotonic(self) -> bool:
        return all(
            b > a for a, b in zip(
                self.frequencies_hz, self.frequencies_hz[1:]
            )
        )

    def out_of_bounds(self) -> list[float]:
        return [
            c for c in self.coefficients
            if c < 0.0 or c > _ABSORPTION_SANITY_MAX
        ]

    def missing_bands(self, required: tuple[float, ...]) -> list[float]:
        have = set(self.frequencies_hz)
        return [f for f in required if f not in have]


class DerivedRatings(BaseModel):
    """NRC/SAA-style derived values — labelled derived, never recomputed."""

    model_config = ConfigDict(frozen=True)

    nrc: float | None = None
    nrc_basis: str | None = None
    saa: float | None = None
    formula_version: str | None = None
    source_standard: str | None = None


class AcousticIndexVariant(BaseModel):
    """One ``absorption_iso354`` measurement variant of a product."""

    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(min_length=1)
    measuring_option: str | None = None
    source_kind: str | None = None
    notes: str | None = None
    # build-up identity — what distinguishes this measurement
    buildup: dict[str, Any] = Field(default_factory=dict)
    alpha_s_oct: AbsorptionSeries | None = None
    alpha_s_terz: AbsorptionSeries | None = None
    alpha_p_oct: AbsorptionSeries | None = None
    calculated_absorption: Any | None = None
    derived: DerivedRatings | None = None
    variant_meta: dict[str, Any] = Field(default_factory=dict)
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'AcousticIndexVariant':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'acoustic index variant semantic hash mismatch'
            )
        return self

    def buildup_fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical(self.buildup).encode('utf-8')
        ).hexdigest()[:16]


class AcousticIndexSnapshot(BaseModel):
    """Immutable verbatim capture of one product's compare response."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['acoustic-index-1'] = (
        ACOUSTIC_INDEX_AUTHORITY_VERSION
    )
    snapshot_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_label: str = Field(min_length=1)
    commercial_name: str | None = None
    manufacturer_name: str | None = None
    material_category: str | None = None
    data_source: str | None = None
    alpha_w: float | None = None
    alpha_w_min: float | None = None
    installation_types: tuple[str, ...] = ()
    structured_meta: dict[str, Any] = Field(default_factory=dict)
    variants: tuple[AcousticIndexVariant, ...]
    api_endpoint: str = Field(min_length=1)
    fetched_utc: str = Field(min_length=1)
    response_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    supersedes_snapshot_id: str | None = None
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'AcousticIndexSnapshot':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'acoustic index snapshot semantic hash mismatch'
            )
        return self


def _series(raw: Any) -> AbsorptionSeries | None:
    """Turn a `{frequency: coefficient}` map into a sorted series."""
    if not isinstance(raw, dict) or not raw:
        return None
    pairs: list[tuple[float, float]] = []
    for key, value in raw.items():
        pairs.append((float(key), float(value)))
    pairs.sort(key=lambda kv: kv[0])
    return AbsorptionSeries(
        frequencies_hz=tuple(f for f, _ in pairs),
        coefficients=tuple(c for _, c in pairs),
    )


def snapshot_from_compare_response(
    *,
    product: dict[str, Any],
    fetched_utc: str,
    api_endpoint: str = ACOUSTIC_INDEX_PUBLIC_COMPARE,
    supersedes_snapshot_id: str | None = None,
) -> AcousticIndexSnapshot:
    """Snapshot one product object from a compare-products response.

    The raw product dict is hashed verbatim; fields are transcribed
    one-for-one so the upstream evidence tier is preserved exactly.
    """
    response_sha256 = hashlib.sha256(
        _canonical(product).encode('utf-8')
    ).hexdigest()
    product_id = str(product.get('id', ''))
    variants: list[AcousticIndexVariant] = []
    for entry in product.get('absorption_iso354') or []:
        buildup = dict(entry.get('variantMeta') or {})
        # merge any structured build-up attributes present on the entry
        for key in ('backing', 'surface', 'distance', 'mounting',
                    'thickness', 'air_gap', 'construction'):
            if key in entry and key not in buildup:
                buildup[key] = entry[key]
        derived_raw = entry.get('derived') or {}
        derived = DerivedRatings(
            nrc=derived_raw.get('nrc'),
            nrc_basis=derived_raw.get('nrcBasis'),
            saa=derived_raw.get('saa'),
            formula_version=derived_raw.get('formulaVersion'),
            source_standard=derived_raw.get('sourceStandard'),
        ) if derived_raw else None
        probe = AcousticIndexVariant.model_construct(**canonicalize_payload(AcousticIndexVariant, dict(
            variant_id=str(entry.get('id', '')),
            measuring_option=entry.get('measuringOption'),
            source_kind=entry.get('sourceKind'),
            notes=entry.get('notes'),
            buildup=buildup,
            alpha_s_oct=_series(entry.get('alphaSOct')),
            alpha_s_terz=_series(entry.get('alphaSTerz')),
            alpha_p_oct=_series(entry.get('alphaPOct')),
            calculated_absorption=entry.get('calculatedAbsorption'),
            derived=derived,
            variant_meta=dict(entry.get('variantMeta') or {}),
            semantic_sha256='',
        )))
        variants.append(
            AcousticIndexVariant(
                **probe.model_dump(
                    mode='python', exclude={'semantic_sha256'}
                ),
                semantic_sha256=_hash(probe.semantic_payload()),
            )
        )
    meta = dict(product.get('structured_meta') or {})
    probe = AcousticIndexSnapshot.model_construct(**canonicalize_payload(AcousticIndexSnapshot, dict(
        schema_version=1,
        authority_version=ACOUSTIC_INDEX_AUTHORITY_VERSION,
        snapshot_id=(
            f"ai-snapshot/{product_id}/{response_sha256[:12]}"
        ),
        product_id=product_id,
        product_label=str(product.get('label', '')),
        commercial_name=product.get('commercial_name'),
        manufacturer_name=product.get('manufacturerName'),
        material_category=product.get('material_category'),
        data_source=product.get('data_source'),
        alpha_w=product.get('alphaW'),
        alpha_w_min=product.get('alphaWMin'),
        installation_types=tuple(product.get('installation_types') or ()),
        structured_meta=meta,
        variants=tuple(variants),
        api_endpoint=api_endpoint,
        fetched_utc=fetched_utc,
        response_sha256=response_sha256,
        supersedes_snapshot_id=supersedes_snapshot_id,
        semantic_sha256='',
    )))
    return AcousticIndexSnapshot(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Import validation — fail closed, no silent repair
# ---------------------------------------------------------------------------

# ISO octave mid-band centres a complete alphaSOct series is expected to
# cover (ISO 354 reporting range).
ISO354_OCTAVE_BANDS_HZ = (100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0,
                        500.0, 630.0, 800.0, 1000.0, 1250.0, 1600.0,
                        2000.0, 2500.0, 3150.0, 4000.0, 5000.0)


def validate_snapshot_for_import(
    snapshot: AcousticIndexSnapshot,
) -> tuple[str, ...]:
    """Fail-closed import checks. Raises; returns nothing on success."""
    problems: list[str] = []
    if not snapshot.variants:
        problems.append('snapshot has no absorption variants')
    for variant in snapshot.variants:
        label = f'variant {variant.variant_id}'
        if not variant.buildup:
            problems.append(
                f'{label}: no build-up identity (thickness/air-gap/'
                'mounting absent) — cannot distinguish this measurement'
            )
        for series_name, series in (
            ('alpha_s_oct', variant.alpha_s_oct),
            ('alpha_s_terz', variant.alpha_s_terz),
            ('alpha_p_oct', variant.alpha_p_oct),
        ):
            if series is None:
                continue
            if not series.is_monotonic():
                problems.append(
                    f'{label}: {series_name} frequency axis is not '
                    'monotonically increasing'
                )
            bad = series.out_of_bounds()
            if bad:
                problems.append(
                    f'{label}: {series_name} coefficients out of bounds '
                    f'{bad[:4]}'
                )
        if variant.alpha_s_oct is not None:
            missing = variant.alpha_s_oct.missing_bands(
                ISO354_OCTAVE_BANDS_HZ
            )
            if missing:
                problems.append(
                    f'{label}: alpha_s_oct missing bands '
                    f'{[int(f) for f in missing[:8]]} Hz'
                )
    if problems:
        raise AcousticIndexImportError(
            'snapshot failed import checks: ' + '; '.join(problems)
        )
    return ()


# ---------------------------------------------------------------------------
# Connector: bounded rebind cache + injected fetcher (offline-safe)
# ---------------------------------------------------------------------------

Fetcher = Callable[[str], Any]


class AcousticIndexConnector:
    """Read-through connector. ``fetcher`` returns parsed JSON for a URL.

    When ``fetcher`` is None the connector is offline: search returns an
    empty non-live result and compare lookups raise
    ``AcousticIndexOfflineError`` unless the product is already cached.
    """

    def __init__(
        self,
        fetcher: Fetcher | None,
        *,
        max_entries: int = 64,
    ) -> None:
        self._fetcher = fetcher
        self._max = max(1, max_entries)
        self._snapshots: OrderedDict[str, AcousticIndexSnapshot] = (
            OrderedDict()
        )

    # -- discovery -----------------------------------------------------------

    def search(
        self, query: str, *, retrieved_utc: str
    ) -> AcousticIndexSearchResult:
        """Public search — returns candidates, not evidence."""
        if self._fetcher is None:
            return AcousticIndexSearchResult(
                query=query,
                retrieved_utc=retrieved_utc,
                live_fetch=False,
                candidates=(),
            )
        url = f'{ACOUSTIC_INDEX_PUBLIC_SEARCH}?q={query}'
        raw = self._fetcher(url)
        items = raw.get('items', raw) if isinstance(raw, dict) else raw
        candidates = tuple(
            AcousticIndexCandidate(
                product_id=str(item.get('id', '')),
                label=str(item.get('label', '')),
                commercial_name=item.get('commercial_name'),
                manufacturer_name=item.get('manufacturerName'),
                material_category=item.get('material_category'),
                data_source=item.get('data_source'),
                alpha_w=item.get('alphaW'),
                alpha_w_min=item.get('alphaWMin'),
            )
            for item in items
        )
        return AcousticIndexSearchResult(
            query=query,
            retrieved_utc=retrieved_utc,
            live_fetch=True,
            candidates=candidates,
        )

    # -- select → snapshot ---------------------------------------------------

    def snapshot_product(
        self, product_id: str, *, fetched_utc: str
    ) -> AcousticIndexSnapshot:
        """Fetch a fresh snapshot (rebinding a stale cache entry)."""
        cached = self._snapshots.get(product_id)
        if self._fetcher is None:
            if cached is not None:
                return cached
            raise AcousticIndexOfflineError(
                f'product {product_id} not cached and fetcher offline'
            )
        url = f'{ACOUSTIC_INDEX_PUBLIC_COMPARE}?id={product_id}'
        raw = self._fetcher(url)
        products = raw.get('items', raw) if isinstance(raw, dict) else raw
        product = products[0] if isinstance(products, list) else products
        snapshot = snapshot_from_compare_response(
            product=product,
            fetched_utc=fetched_utc,
            supersedes_snapshot_id=(
                cached.snapshot_id
                if cached is not None
                and cached.response_sha256
                != hashlib.sha256(
                    _canonical(product).encode('utf-8')
                ).hexdigest()
                else None
            ),
        )
        self._store(product_id, snapshot)
        return snapshot

    def cached_snapshot(
        self, product_id: str
    ) -> AcousticIndexSnapshot | None:
        return self._snapshots.get(product_id)

    def _store(
        self, product_id: str, snapshot: AcousticIndexSnapshot
    ) -> None:
        self._snapshots[product_id] = snapshot
        self._snapshots.move_to_end(product_id)
        while len(self._snapshots) > self._max:
            self._snapshots.popitem(last=False)

    # -- import --------------------------------------------------------------

    def import_snapshot(
        self, snapshot: AcousticIndexSnapshot
    ) -> AcousticIndexSnapshot:
        """Validate and admit a snapshot as importable evidence."""
        validate_snapshot_for_import(snapshot)
        return snapshot


__all__ = [
    'ACOUSTIC_INDEX_API_BASE',
    'ACOUSTIC_INDEX_AUTHORITY_VERSION',
    'ACOUSTIC_INDEX_PUBLIC_COMPARE',
    'ACOUSTIC_INDEX_PUBLIC_SEARCH',
    'AbsorptionSeries',
    'AcousticIndexCandidate',
    'AcousticIndexConnector',
    'AcousticIndexError',
    'AcousticIndexImportError',
    'AcousticIndexOfflineError',
    'AcousticIndexSearchResult',
    'AcousticIndexSnapshot',
    'AcousticIndexVariant',
    'DerivedRatings',
    'ISO354_OCTAVE_BANDS_HZ',
    'snapshot_from_compare_response',
    'validate_snapshot_for_import',
]
