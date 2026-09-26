"""Standards-derived video verification pack (#1075).

Deterministic ITU-R BT.2111-3 HDR colour-bar pattern descriptors plus
admission records for EBU monitor/test material. BT.2111 specifies the
reference test patterns for BT.2100 HDR systems; the code values below
are transcribed verbatim from Rec. ITU-R BT.2111-3 (05/2025), Tables 2
(HLG narrow range), 3 (PQ narrow range) and 4 (PQ full range).

Rules:

- a :class:`PatternDescriptor` carries exact 10-bit (primary) and
  12-bit code values per named image area — generated patterns are
  deterministic functions of the descriptor, never hand-tuned pixels;
- signal range (``narrow``/``full``) is explicit on every descriptor —
  narrow-range code values are never silently used on a full-range
  path;
- this module emits *specifications* (which bars, at which code values,
  in which order); pixel rasterisation lives elsewhere;
- EBU monitor material is admitted download-on-demand with license as
  published by the EBU — the twin never bundles the payloads.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
    external_asset_file,
)


BT2111_PACK_AUTHORITY_VERSION = 'bt2111-video-pack-1'

# Recommendation edition these tables were transcribed from.
BT2111_REFERENCE = 'Rec. ITU-R BT.2111-3 (05/2025)'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


TransferFunction = Literal['hlg', 'pq']
SignalRange = Literal['narrow', 'full']
PatternVariant = Literal['hlg_narrow', 'pq_narrow', 'pq_full']


class PatternArea(BaseModel):
    """One named image area with its exact R'G'B' code values."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    code_10bit: tuple[int, int, int]
    code_12bit: tuple[int, int, int]
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'PatternArea':
        for v in self.code_10bit:
            if not 0 <= v <= 1023:
                raise ValueError('10-bit code value out of range')
        for v in self.code_12bit:
            if not 0 <= v <= 4095:
                raise ValueError('12-bit code value out of range')
        return self


def _area(
    name: str,
    r10: int,
    g10: int,
    b10: int,
    r12: int,
    g12: int,
    b12: int,
    note: str = '',
) -> PatternArea:
    return PatternArea(
        name=name,
        code_10bit=(r10, g10, b10),
        code_12bit=(r12, g12, b12),
        note=note,
    )


# Rec. ITU-R BT.2111-3, Table 2 — HLG, narrow range.
_HLG_NARROW: tuple[PatternArea, ...] = (
    _area('100% White', 940, 940, 940, 3760, 3760, 3760),
    _area('100% Yellow', 940, 940, 64, 3760, 3760, 256),
    _area('100% Cyan', 64, 940, 940, 256, 3760, 3760),
    _area('100% Green', 64, 940, 64, 256, 3760, 256),
    _area('100% Magenta', 940, 64, 940, 3760, 256, 3760),
    _area('100% Red', 940, 64, 64, 3760, 256, 256),
    _area('100% Blue', 64, 64, 940, 256, 256, 3760),
    _area('75% White', 721, 721, 721, 2884, 2884, 2884),
    _area('75% Yellow', 721, 721, 64, 2884, 2884, 256),
    _area('75% Cyan', 64, 721, 721, 256, 2884, 2884),
    _area('75% Green', 64, 721, 64, 256, 2884, 256),
    _area('75% Magenta', 721, 64, 721, 2884, 256, 2884),
    _area('75% Red', 721, 64, 64, 2884, 256, 256),
    _area('75% Blue', 64, 64, 721, 256, 256, 2884),
    _area('40% Grey', 414, 414, 414, 1656, 1656, 1656),
    _area(
        '-7% Step',
        4, 4, 4, 16, 16, 16,
        'minimum permitted narrow-range code value (BT.2100)',
    ),
    _area('0% Step', 64, 64, 64, 256, 256, 256),
    _area('10% Step', 152, 152, 152, 608, 608, 608),
    _area('20% Step', 239, 239, 239, 956, 956, 956),
    _area('30% Step', 327, 327, 327, 1308, 1308, 1308),
    _area('40% Step', 414, 414, 414, 1656, 1656, 1656),
    _area('50% Step', 502, 502, 502, 2008, 2008, 2008),
    _area('60% Step', 590, 590, 590, 2360, 2360, 2360),
    _area('70% Step', 677, 677, 677, 2708, 2708, 2708),
    _area('80% Step', 765, 765, 765, 3060, 3060, 3060),
    _area('90% Step', 852, 852, 852, 3408, 3408, 3408),
    _area('100% Step', 940, 940, 940, 3760, 3760, 3760),
    _area(
        '109% Step',
        1019, 1019, 1019, 4076, 4076, 4076,
        'maximum permitted narrow-range code value (BT.2100)',
    ),
    _area('75% BT.709 Yellow', 713, 719, 316, 2852, 2876, 1264),
    _area('75% BT.709 Cyan', 538, 709, 718, 2152, 2836, 2872),
    _area('75% BT.709 Green', 512, 706, 296, 2048, 2824, 1184),
    _area('75% BT.709 Magenta', 651, 286, 705, 2604, 1144, 2820),
    _area('75% BT.709 Red', 639, 269, 164, 2556, 1076, 656),
    _area('75% BT.709 Blue', 227, 147, 702, 908, 588, 2808),
)

# Rec. ITU-R BT.2111-3, Table 3 — PQ, narrow range.
_PQ_NARROW: tuple[PatternArea, ...] = (
    _area('100% White', 940, 940, 940, 3760, 3760, 3760),
    _area('100% Yellow', 940, 940, 64, 3760, 3760, 256),
    _area('100% Cyan', 64, 940, 940, 256, 3760, 3760),
    _area('100% Green', 64, 940, 64, 256, 3760, 256),
    _area('100% Magenta', 940, 64, 940, 3760, 256, 3760),
    _area('100% Red', 940, 64, 64, 3760, 256, 256),
    _area('100% Blue', 64, 64, 940, 256, 256, 3760),
    _area(
        '58% White', 573, 573, 573, 2292, 2292, 2292,
        'approx. 75% HLG at the 1000 cd/m2 reference level',
    ),
    _area('58% Yellow', 573, 573, 64, 2292, 2292, 256),
    _area('58% Cyan', 64, 573, 573, 256, 2292, 2292),
    _area('58% Green', 64, 573, 64, 256, 2292, 256),
    _area('58% Magenta', 573, 64, 573, 2292, 256, 2292),
    _area('58% Red', 573, 64, 64, 2292, 256, 256),
    _area('58% Blue', 64, 64, 573, 256, 256, 2292),
    _area('40% Grey', 414, 414, 414, 1656, 1656, 1656),
    _area(
        '-7% Step', 4, 4, 4, 16, 16, 16,
        'minimum permitted narrow-range code value (BT.2100)',
    ),
    _area('0% Step', 64, 64, 64, 256, 256, 256),
    _area('10% Step', 152, 152, 152, 608, 608, 608),
    _area('20% Step', 239, 239, 239, 956, 956, 956),
    _area('30% Step', 327, 327, 327, 1308, 1308, 1308),
    _area('40% Step', 414, 414, 414, 1656, 1656, 1656),
    _area('50% Step', 502, 502, 502, 2008, 2008, 2008),
    _area('60% Step', 590, 590, 590, 2360, 2360, 2360),
    _area('70% Step', 677, 677, 677, 2708, 2708, 2708),
    _area('80% Step', 765, 765, 765, 3060, 3060, 3060),
    _area('90% Step', 852, 852, 852, 3408, 3408, 3408),
    _area('100% Step', 940, 940, 940, 3760, 3760, 3760),
    _area(
        '109% Step', 1019, 1019, 1019, 4076, 4076, 4076,
        'maximum permitted narrow-range code value (BT.2100)',
    ),
    _area('58% BT.709 Yellow', 569, 572, 381, 2276, 2288, 1524),
    _area('58% BT.709 Cyan', 485, 566, 571, 1940, 2264, 2284),
    _area('58% BT.709 Green', 474, 565, 368, 1896, 2260, 1472),
    _area('58% BT.709 Magenta', 537, 362, 564, 2148, 1448, 2256),
    _area('58% BT.709 Red', 531, 351, 257, 2124, 1404, 1028),
    _area('58% BT.709 Blue', 318, 236, 563, 1272, 944, 2252),
    _area('0% Black', 64, 64, 64, 256, 256, 256),
    _area(
        '-2% Black', 48, 48, 48, 192, 192, 192,
        "BT.814 'slightly darker level'",
    ),
    _area(
        '+2% Black', 80, 80, 80, 320, 320, 320,
        "BT.814 'slightly lighter level'",
    ),
    _area('+4% Black', 99, 99, 99, 396, 396, 396),
)

# Rec. ITU-R BT.2111-3, Table 4 — PQ, full range.
_PQ_FULL: tuple[PatternArea, ...] = (
    _area('100% White', 1023, 1023, 1023, 4095, 4095, 4095),
    _area('100% Yellow', 1023, 1023, 0, 4095, 4095, 0),
    _area('100% Cyan', 0, 1023, 1023, 0, 4095, 4095),
    _area('100% Green', 0, 1023, 0, 0, 4095, 0),
    _area('100% Magenta', 1023, 0, 1023, 4095, 0, 4095),
    _area('100% Red', 1023, 0, 0, 4095, 0, 0),
    _area('100% Blue', 0, 0, 1023, 0, 0, 4095),
    _area('30% Step', 307, 307, 307, 1229, 1229, 1229),
    _area('40% Step', 409, 409, 409, 1638, 1638, 1638),
    _area('50% Step', 512, 512, 512, 2048, 2048, 2048),
    _area('60% Step', 614, 614, 614, 2457, 2457, 2457),
    _area('70% Step', 716, 716, 716, 2867, 2867, 2867),
    _area('80% Step', 818, 818, 818, 3276, 3276, 3276),
    _area('90% Step', 921, 921, 921, 3686, 3686, 3686),
    _area('100% Step', 1023, 1023, 1023, 4095, 4095, 4095),
    _area('58% BT.709 Yellow', 589, 593, 370, 2359, 2373, 1483),
    _area('58% BT.709 Cyan', 491, 586, 592, 1967, 2348, 2371),
    _area('58% BT.709 Green', 479, 585, 355, 1918, 2342, 1423),
    _area('58% BT.709 Magenta', 552, 348, 584, 2209, 1391, 2339),
    _area('58% BT.709 Red', 545, 335, 225, 2181, 1339, 901),
    _area('58% BT.709 Blue', 296, 201, 582, 1186, 806, 2331),
    _area('0% Black', 0, 0, 0, 0, 0, 0),
    _area(
        '+2% Black', 19, 19, 19, 75, 75, 75,
        "BT.814 'slightly lighter level'",
    ),
    _area('+4% Black', 41, 41, 41, 164, 164, 164),
)


class PatternDescriptor(BaseModel):
    """A complete deterministic descriptor for one BT.2111 variant."""

    model_config = ConfigDict(frozen=True)

    variant: PatternVariant
    transfer_function: TransferFunction
    signal_range: SignalRange
    bit_depth_primary: Literal[10, 12] = 10
    reference: str = Field(min_length=1)
    areas: tuple[PatternArea, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'PatternDescriptor':
        expected_range = {
            'hlg_narrow': 'narrow',
            'pq_narrow': 'narrow',
            'pq_full': 'full',
        }[self.variant]
        if self.signal_range != expected_range:
            raise ValueError(
                f'{self.variant} requires signal_range='
                f'{expected_range!r}'
            )
        expected_tf = {
            'hlg_narrow': 'hlg',
            'pq_narrow': 'pq',
            'pq_full': 'pq',
        }[self.variant]
        if self.transfer_function != expected_tf:
            raise ValueError(
                f'{self.variant} requires transfer_function='
                f'{expected_tf!r}'
            )
        names = [a.name for a in self.areas]
        if len(names) != len(set(names)):
            raise ValueError('duplicate area names')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('pattern descriptor semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    def area(self, name: str) -> PatternArea | None:
        for a in self.areas:
            if a.name == name:
                return a
        return None


def _descriptor(
    variant: PatternVariant,
    transfer: TransferFunction,
    signal_range: SignalRange,
    areas: tuple[PatternArea, ...],
) -> PatternDescriptor:
    probe = PatternDescriptor.model_construct(
        variant=variant,
        transfer_function=transfer,
        signal_range=signal_range,
        bit_depth_primary=10,
        reference=BT2111_REFERENCE,
        areas=areas,
        semantic_sha256='',
    )
    return PatternDescriptor(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


BT2111_HLG_NARROW: PatternDescriptor = _descriptor(
    'hlg_narrow', 'hlg', 'narrow', _HLG_NARROW
)
BT2111_PQ_NARROW: PatternDescriptor = _descriptor(
    'pq_narrow', 'pq', 'narrow', _PQ_NARROW
)
BT2111_PQ_FULL: PatternDescriptor = _descriptor(
    'pq_full', 'pq', 'full', _PQ_FULL
)

BT2111_PATTERNS: dict[PatternVariant, PatternDescriptor] = {
    'hlg_narrow': BT2111_HLG_NARROW,
    'pq_narrow': BT2111_PQ_NARROW,
    'pq_full': BT2111_PQ_FULL,
}


def pattern_for(variant: PatternVariant) -> PatternDescriptor:
    return BT2111_PATTERNS[variant]


class RangeCheck(NamedTuple):
    compatible: bool
    detail: str


def check_signal_range(
    descriptor: PatternDescriptor, declared_range: SignalRange
) -> RangeCheck:
    """A narrow-range pattern can never be emitted on a full-range
    path without an explicit range conversion — which HTDT does not do."""
    if descriptor.signal_range != declared_range:
        return RangeCheck(
            False,
            f'pattern is {descriptor.signal_range} range; path '
            f'declared {declared_range}',
        )
    return RangeCheck(True, 'signal ranges match')


# --- EBU monitor/test material admission ----------------------------------

EBU_MONITOR_MATERIAL_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='ebu-monitor-test-material',
        dataset_name='ebu-monitor-test',
        dataset_title='EBU monitor & video test material (tech.ebu.ch)',
        publisher='European Broadcasting Union',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        record_uri='https://tech.ebu.ch/testmaterial',
        license_id='ebu-test-material',
        license_family='unknown',
        license_note=(
            'EBU publishes monitor/test sequences for download at '
            'tech.ebu.ch; redistribution terms are governed by the EBU '
            'site license — payload bytes are user-downloaded, never '
            'bundled into HTDT.'
        ),
        files=(),
        dataset_notes=(
            'Monitor-evaluation and video test sequences published by '
            'the EBU. Exact file inventory is resolved at download '
            'time; checksums are recorded into the ledger once the '
            'publisher publishes them.'
        ),
    )
)

VIDEO_VERIFICATION_ADMISSIONS: tuple[ExternalAssetAdmission, ...] = (
    EBU_MONITOR_MATERIAL_ADMISSION,
)


__all__ = [
    'BT2111_HLG_NARROW',
    'BT2111_PACK_AUTHORITY_VERSION',
    'BT2111_PATTERNS',
    'BT2111_PQ_FULL',
    'BT2111_PQ_NARROW',
    'BT2111_REFERENCE',
    'EBU_MONITOR_MATERIAL_ADMISSION',
    'PatternArea',
    'PatternDescriptor',
    'PatternVariant',
    'RangeCheck',
    'SignalRange',
    'TransferFunction',
    'VIDEO_VERIFICATION_ADMISSIONS',
    'check_signal_range',
    'pattern_for',
]
