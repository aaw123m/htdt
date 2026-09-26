"""Yamaha RX-A4A evidence-first adapter qualification (#1057).

This module is a *qualification packet*, not a live adapter: it encodes
what official Yamaha sources establish about the RX-A4A before any
hardware access, so the eventual adapter lands pre-bounded.

Authority discipline (per the issue contract):

- documented product capability is never promoted to an API capability —
  the matrix rows keep ``tier=DOCUMENTED_ONLY`` / ``UNKNOWN`` until an
  interface actually proves the field;
- mirrored Yamaha Extended Control (YXC) specifications are
  ``RESEARCH_ONLY``: no official Yamaha-hosted spec qualifying
  RX-A4A firmware 2.26 has been located, so live-API claims stay
  ``experimental_undocumented``/``UNKNOWN`` pending legal review and an
  owned-hardware session;
- qualification is firmware-scoped (``2.26``, released 2026-09-15);
  older-firmware rows are historical and never silently inherited —
  ``resolve_firmware_capability`` (#792) already returns
  ``NEEDS_REQUALIFICATION`` for that case;
- ``MC_backup_*`` payloads are stored content-addressed *unparsed* —
  format legality/semantics are unreviewed.

Fixtures here are sanitized: no real MAC/IP/serial/account data.
See ``docs/ISSUE_1057_YAMAHA_RXA4A_QUALIFICATION_*.md``.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_device_compatibility import (
    CapabilityTier,
    DeviceCompatibilityMatrix,
    DeviceCompatibilityRow,
    InterfaceProvenance,
    NormalizedDeviceCapability,
)


RXA4A_MODEL = 'RX-A4A'
RXA4A_MANUFACTURER = 'Yamaha'
#: Current official firmware per Yamaha's update page (2026-09-15).
RXA4A_CURRENT_FIRMWARE = '2.26'
#: Candidate transport once a live adapter is authorized — mirrored YXC
#: over local HTTP; NOT official-source-confirmed for 2.26.
RXA4A_CANDIDATE_TRANSPORT = 'yxc-local-http'
#: Planned adapter id (the live adapter itself is a later hardware gate).
RXA4A_PLANNED_ADAPTER_ID = 'htdt-yamaha-rxa4a'
RXA4A_PLANNED_ADAPTER_VERSION = '0'


class YamahaSourceEntry(BaseModel):
    """One official-source inventory row for the qualification packet."""

    model_config = ConfigDict(frozen=True)

    source_url: str = Field(min_length=1)
    source_date: str = Field(min_length=1)
    language: str = 'en'
    claim_family: str = Field(min_length=1)
    currency: Literal['current', 'superseded', 'research_only'] = 'current'

    @model_validator(mode='after')
    def no_credentials(self) -> 'YamahaSourceEntry':
        if any(
            marker in self.source_url.lower()
            for marker in ('password', 'token', 'sessionid')
        ):
            raise ValueError('source urls never carry credentials')
        return self


class FirmwareHistoryEntry(BaseModel):
    """One firmware release mapped to affected capability families."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1)
    released: str = Field(min_length=1)
    affected_families: tuple[str, ...]
    stale_after: str | None = None
    note: str = ''


#: Official firmware history → capability families it touched, per
#: Yamaha's update page. Rows older than the current firmware are
#: ``stale_after``-marked so a stored qualification is never silently
#: inherited forward.
RXA4A_FIRMWARE_HISTORY: tuple[FirmwareHistoryEntry, ...] = (
    FirmwareHistoryEntry(
        version='1.65',
        released='2023-12-07',
        affected_families=('video_hdmi', 'hdr10+', '8k60_4k120'),
        stale_after='2.26',
        note='8K60/4K120 and HDR10+ passthrough changes',
    ),
    FirmwareHistoryEntry(
        version='1.73',
        released='2024-03-21',
        affected_families=('video_hdmi', 'upmixing', 'vrr_allm'),
        stale_after='2.26',
        note='VRR/ALLM and cross-format upmix behavior',
    ),
    FirmwareHistoryEntry(
        version='2.02',
        released='2024-10-03',
        affected_families=('network_source', 'qobuz'),
        stale_after='2.26',
        note='Qobuz support changes',
    ),
    FirmwareHistoryEntry(
        version='2.12',
        released='2025-04-10',
        affected_families=('network_source', 'works_with_sonos'),
        stale_after='2.26',
        note='Works with Sonos changes',
    ),
    FirmwareHistoryEntry(
        version='2.24',
        released='2025-11-20',
        affected_families=(
            'network_source',
            'works_with_sonos',
            'voice_assistants',
        ),
        stale_after='2.26',
        note='stability fixes; Alexa Skills/Google Actions removed; Sonos volume control',
    ),
    FirmwareHistoryEntry(
        version='2.26',
        released='2026-09-15',
        affected_families=('works_with_sonos',),
        note='Works with Sonos bug fix — current',
    ),
)


#: Official-source inventory (section 3 of the issue). URLs are Yamaha
#: primary material; sibling-model manual pages are explicitly marked
#: ``research_only`` until confirmed identical on RX-A4A.
RXA4A_SOURCE_INVENTORY: tuple[YamahaSourceEntry, ...] = (
    YamahaSourceEntry(
        source_url='https://jp.yamaha.com/products/audio_visual/av_receivers_amps/rx-a4a/index.html',
        source_date='2026-09-25',
        claim_family='product_capability',
        currency='current',
    ),
    YamahaSourceEntry(
        source_url='https://jp.yamaha.com/products/audio_visual/av_receivers_amps/rx-a4a/specs.html',
        source_date='2026-09-25',
        claim_family='specification',
        currency='current',
    ),
    YamahaSourceEntry(
        source_url='https://jp.yamaha.com/support/updates/rxa4a.html',
        source_date='2026-09-25',
        claim_family='firmware',
        currency='current',
    ),
    YamahaSourceEntry(
        source_url='https://manual.yamaha.com/av/20/rxa4a/en-US/316973579.html',
        source_date='2026-09-25',
        claim_family='backup_restore',
        currency='current',
    ),
    YamahaSourceEntry(
        source_url='https://manual.yamaha.com/av/20/rxv4a/en-US/6025089803.html',
        source_date='2026-09-25',
        claim_family='backup_restore',
        currency='research_only',
    ),
)


#: Whole-device backup semantics per the RX-A4A manual (sibling-model
#: filename detail marked research-only until confirmed identical).
RXA4A_BACKUP_SEMANTICS: dict[str, Any] = {
    'menu_path': 'Setup -> System -> Backup/Restore (front-panel/web setup per manual)',
    'medium': 'USB storage FAT16/FAT32; a PC-backup path is documented separately',
    'filename_pattern': 'MC_backup_<model>.dat (research_only: sibling-model page)',
    'overwrite_in_place': False,
    'excluded_data': ('user account information', 'passwords'),
    'restore_requires_reboot': True,
    'format_status': 'unreviewed_opaque',
    'storage_policy': 'content-addressed raw storage only; no parsing until legality/semantics review and a user-owned sample exist',
}


#: One row per (capability, direction) the packet qualifies. Everything
#: is DOCUMENTED_ONLY at best: manual-documented capability is product
#: truth, not adapter truth; mirrored YXC surfaces stay UNKNOWN until an
#: official source or owned hardware proves them.
_RXA4A_ROWS: tuple[
    tuple[NormalizedDeviceCapability, Literal['read', 'write', 'export'], CapabilityTier, InterfaceProvenance, str],
    ...
] = (
    ('discover', 'read', 'UNKNOWN', 'experimental_undocumented',
     'no official discovery spec confirmed for firmware 2.26; mDNS/MusicCast research-only'),
    ('bind_exact_target', 'read', 'UNKNOWN', 'experimental_undocumented',
     'requires official source review + owned hardware'),
    ('firmware_model_readback', 'read', 'UNKNOWN', 'experimental_undocumented',
     'mirrored YXC getSystemInfo covers model/version — unofficial'),
    ('power', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'manual documents power control; interface unproven'),
    ('power', 'write', 'UNKNOWN', 'experimental_undocumented',
     'write path deferred until interface source review'),
    ('input_select', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'manual documents input selection; interface unproven'),
    ('input_select', 'write', 'UNKNOWN', 'experimental_undocumented',
     'write path deferred until interface source review'),
    ('mute', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual', ''),
    ('mute', 'write', 'UNKNOWN', 'experimental_undocumented',
     'write path deferred until interface source review'),
    ('master_volume', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual', ''),
    ('master_volume', 'write', 'UNKNOWN', 'experimental_undocumented',
     'write path deferred until interface source review'),
    ('decoder_mode', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'sound/decoder programs documented; field-level read unproven'),
    ('speaker_configuration', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'speaker layout is a manual capability; API field unproven'),
    ('crossover', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'bass-management settings documented; readback unproven'),
    ('distance_delay', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'speaker distance/delay documented; readback unproven'),
    ('trim_gain', 'read', 'DOCUMENTED_ONLY', 'user_attested_manual',
     'level trims documented; readback unproven'),
    ('peq', 'read', 'UNKNOWN', 'experimental_undocumented',
     'YPAO PEQ readback not documented on official material'),
    ('command_ack', 'read', 'UNKNOWN', 'experimental_undocumented',
     'ACK semantics from mirrored YXC only; unproven'),
    ('readback', 'read', 'UNKNOWN', 'experimental_undocumented',
     'exact read-back semantics unproven on 2.26'),
    ('exact_setting_comparison', 'read', 'UNKNOWN', 'experimental_undocumented', ''),
    ('drift_detection', 'read', 'UNKNOWN', 'experimental_undocumented', ''),
)


def build_rxa4a_capability_matrix(
    *,
    matrix_id: str,
    created_at_utc: str,
) -> DeviceCompatibilityMatrix:
    """Firmware-2.26 qualification rows for the planned RX-A4A adapter.

    Rows are the packet's machine-readable half: every capability is
    DOCUMENTED_ONLY at best — nothing here asserts the interface works.
    """
    rows = tuple(
        DeviceCompatibilityRow(
            adapter_id=RXA4A_PLANNED_ADAPTER_ID,
            adapter_version=RXA4A_PLANNED_ADAPTER_VERSION,
            manufacturer=RXA4A_MANUFACTURER,
            model=RXA4A_MODEL,
            firmware_version=RXA4A_CURRENT_FIRMWARE,
            transport=RXA4A_CANDIDATE_TRANSPORT,
            capability=capability,
            direction=direction,
            tier=tier,
            interface_provenance=provenance,
            qualified_at_utc=created_at_utc,
            notes=notes or 'firmware-2.26 qualification packet row',
        )
        for capability, direction, tier, provenance, notes in _RXA4A_ROWS
    )
    payload: dict[str, Any] = {
        'matrix_id': matrix_id,
        'created_at_utc': created_at_utc,
        'rows': rows,
    }
    provisional = DeviceCompatibilityMatrix.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return DeviceCompatibilityMatrix(
        **payload, semantic_sha256=_packet_hash(provisional.identity_payload())
    )


def _packet_hash(payload: Any) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()


# ----------------------------------------------------------------------
# Sanitized fixtures — documented-shape payloads, no real identity data.

RXA4A_FIXTURES: dict[str, dict[str, Any]] = {
    # Mirrored-YXC-shaped getSystemInfo response (RESEARCH_ONLY shape,
    # sanitized) — establishes what a read-back could look like.
    'get_system_info_ok': {
        'response_code': 0,
        'model_name': 'RX-A4A',
        'destination': 'B',
        'system_id': '00000000',
        'system_version': 2.26,
        'api_version': 2.17,
        'netmodule_generation': 3,
        'netmodule_version': '0000.00.00',
        'netmodule_checksum': 'AAAAAAAA',
        'operation_mode': 'normal',
        'update_error_code': '00000000',
    },
    'get_system_info_firmware_mismatch': {
        'response_code': 0,
        'model_name': 'RX-A4A',
        'destination': 'B',
        'system_id': '00000000',
        'system_version': 1.73,
        'api_version': 2.08,
        'operation_mode': 'normal',
    },
    'get_system_info_model_mismatch': {
        'response_code': 0,
        'model_name': 'RX-V6A',
        'system_id': '00000000',
        'system_version': 2.26,
        'api_version': 2.17,
        'operation_mode': 'normal',
    },
    # Response envelopes for negative cases (documented YXC response_code
    # semantics in the mirrored spec; sanitized).
    'unsupported_field_error': {
        'response_code': 112,
        'detail': 'unsupported function (mirrored-YXC error envelope)',
    },
    'malformed_response': '<<<not json>>>',
    'ack_only_no_readback': {
        'response_code': 0,
        'note': 'command ACK without a subsequent state read-back is not verified state',
    },
}


#: Single consolidated hardware-session checklist (issue section 8) —
#: everything else stays on GitHub/CI.
RXA4A_HARDWARE_GATE_CHECKLIST: tuple[str, ...] = (
    'discover and bind the exact owned RX-A4A (serial/endpoint proof)',
    'read back firmware + model + api version',
    'command-level capability probe (read-only fields only)',
    'confirm whole-device backup generation + filename on owned unit',
    'one approved non-destructive write (e.g. power standby) + read-back',
    'disconnect/reconnect behavior',
)


__all__ = [
    'RXA4A_BACKUP_SEMANTICS',
    'RXA4A_CANDIDATE_TRANSPORT',
    'RXA4A_CURRENT_FIRMWARE',
    'RXA4A_FIRMWARE_HISTORY',
    'RXA4A_FIXTURES',
    'RXA4A_HARDWARE_GATE_CHECKLIST',
    'RXA4A_MANUFACTURER',
    'RXA4A_MODEL',
    'RXA4A_PLANNED_ADAPTER_ID',
    'RXA4A_SOURCE_INVENTORY',
    'FirmwareHistoryEntry',
    'YamahaSourceEntry',
    'build_rxa4a_capability_matrix',
]
