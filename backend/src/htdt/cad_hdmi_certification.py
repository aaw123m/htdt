"""HDMI cable certification evidence capture (#1078).

Binds official HDMI Licensing certification evidence — the QR-label scan
(ULTRA HIGH SPEED HDMI CABLE / Premium HDMI Cable Certification label
hologram scan) plus the manufacturer's published certification page —
to an installed :class:`~.cad_cable_run.CableRun`.

Hard rules:

- certification evidence is bound to a cable *model* and verifies the
  product line was tested — it is **never** a statement about the
  installed run's field reliability; ``field_reliability`` is a
  separate literal and stays ``not_evaluated`` unless a real installed
  measurement is recorded elsewhere;
- a QR payload is stored verbatim (``payload_sha256`` over raw bytes) —
  we never fabricate a verification reply the label scan didn't
  produce;
- binding is fail-closed: a certification only ``compatible`` when the
  program's class matches the run's required class and the certified
  nominal length covers the installed run length; anything else is a
  reported non-verdict, not a silent pass.
"""

from __future__ import annotations

import hashlib
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_cable_run import CableRun


HDMI_CERT_AUTHORITY_VERSION = 'hdmi-certification-1'


HdmiCertificationProgram = Literal[
    'ultra_high_speed',
    'premium_high_speed',
    'high_speed',
    'standard',
]
"""HDMI cable certification programs (HDMI Licensing)."""

FieldReliabilityState = Literal[
    'not_evaluated',
    'verified_in_situ',
    'failed_in_situ',
]
"""Field reliability of the *installed* cable — independent of product
certification. Certification never upgrades this."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class CableCertificationRecord(BaseModel):
    """Certification evidence for one cable model, verbatim-pinned."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    program: HdmiCertificationProgram
    brand: str = Field(min_length=1)
    model: str = Field(min_length=1)
    qr_payload: str = Field(min_length=1)
    """The exact scanned QR payload (typically the HDMI.org /
    LA verification URL) — stored verbatim, never normalized away."""
    qr_payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    certified_lengths_m: tuple[float, ...] = ()
    """Nominal lengths the manufacturer lists as certified for this
    model — the certification covers the listed lengths only."""
    verification_uri: str | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'CableCertificationRecord':
        if self.qr_payload_sha256 != _sha256_text(self.qr_payload):
            raise ValueError(
                'qr_payload_sha256 must hash the verbatim payload'
            )
        return self


def build_certification_record(
    *,
    record_id: str,
    program: HdmiCertificationProgram,
    brand: str,
    model: str,
    qr_payload: str,
    certified_lengths_m: tuple[float, ...] = (),
    verification_uri: str | None = None,
    notes: str = '',
) -> CableCertificationRecord:
    return CableCertificationRecord(
        record_id=record_id,
        program=program,
        brand=brand,
        model=model,
        qr_payload=qr_payload,
        qr_payload_sha256=_sha256_text(qr_payload),
        certified_lengths_m=certified_lengths_m,
        verification_uri=verification_uri,
        notes=notes,
    )


CertificationBinding = Literal[
    'compatible',
    'length_uncovered',
    'program_insufficient',
    'unverifiable_payload',
]
"""Fail-closed binding verdicts."""


_PROGRAM_RANK: dict[HdmiCertificationProgram, int] = {
    'standard': 0,
    'high_speed': 1,
    'premium_high_speed': 2,
    'ultra_high_speed': 3,
}


class InstalledCableCertification(BaseModel):
    """A certification record bound to one installed CableRun."""

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    verdict: CertificationBinding
    field_reliability: FieldReliabilityState = 'not_evaluated'
    detail: str = ''


def bind_certification(
    record: CableCertificationRecord,
    run: CableRun,
    *,
    required_program: HdmiCertificationProgram,
    binding_id: str,
) -> InstalledCableCertification:
    """Bind certification evidence to an installed cable run.

    Verdict rules (all fail-closed):

    - ``unverifiable_payload`` — QR payload empty/unparseable shape;
    - ``program_insufficient`` — certified program class below the
      run's required class;
    - ``length_uncovered`` — the record lists certified nominal
      lengths and the installed run is longer than every one;
    - ``compatible`` — none of the above. Still **not** a field
      reliability statement: ``field_reliability`` remains
      ``not_evaluated`` until a real in-situ check is recorded.
    """
    payload = record.qr_payload.strip()
    if not payload or ' ' in payload:
        verdict: CertificationBinding = 'unverifiable_payload'
        detail = 'QR payload is not a parseable verification token'
    elif _PROGRAM_RANK[record.program] < _PROGRAM_RANK[required_program]:
        verdict = 'program_insufficient'
        detail = (
            f'certified program {record.program} does not cover '
            f'required {required_program}'
        )
    elif record.certified_lengths_m and all(
        run.total_length_m > L + 1e-9
        for L in record.certified_lengths_m
    ):
        verdict = 'length_uncovered'
        detail = (
            f'run length {run.total_length_m} m exceeds every '
            'certified nominal length'
        )
    else:
        verdict = 'compatible'
        detail = (
            'program and length consistent with certification; '
            'field reliability not evaluated'
        )
    return InstalledCableCertification(
        binding_id=binding_id,
        run_id=run.run_id,
        record_id=record.record_id,
        verdict=verdict,
        field_reliability='not_evaluated',
        detail=detail,
    )


__all__ = [
    'CableCertificationRecord',
    'CertificationBinding',
    'FieldReliabilityState',
    'HDMI_CERT_AUTHORITY_VERSION',
    'HdmiCertificationProgram',
    'InstalledCableCertification',
    'bind_certification',
    'build_certification_record',
]
