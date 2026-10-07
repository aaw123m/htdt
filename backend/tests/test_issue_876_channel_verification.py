"""#876 automated channel identity / routing / polarity verification.

Per-channel excitation through the #869 sweep engine, signature analysis,
the five-class evidence ladder (machine_verified_routing /
machine_verified_relative_polarity / machine_assisted_ambiguous /
operator_attested / unknown), plan-wide duplicate detection, targeted
staleness on routing change, and sealed persistence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_channel_identity_authority import build_channel_identity_chain
from htdt.cad_channel_verification import (
    ChannelVerificationPlan,
    OperatorChannelAttestation,
    VerificationThresholds,
    build_verification_plan,
    evaluate_channel_verdicts,
    plan_binding,
    response_signature,
    routing_signature,
    run_channel_excitation,
    run_verification_plan,
    stale_channels_for_routing,
)
from htdt.cad_channel_verification_repository import (
    CadChannelVerificationRepository,
    ChannelVerificationConflictError,
    ChannelVerificationIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.cad_sweep_acquisition import (
    AcquisitionRequest,
    ArmConfirmation,
    ChannelRouting,
    FakeAudioBackend,
    default_fake_scenario,
)
from htdt.cad_sweep_acquisition_repository import (
    CadSweepAcquisitionRepository,
)

DOC = 'doc-876'
_TS = '2026-10-07T00:00:00Z'
_TS2 = '2026-10-07T00:00:01Z'

SAMPLE_RATE = 48000


def _routing(channel: int, loopback: int | None = 1) -> ChannelRouting:
    return ChannelRouting(
        playback_device_id='fake-duplex-0',
        playback_channel=channel,
        capture_device_id='fake-duplex-0',
        capture_channel=0,
        loopback_input_channel=loopback,
    )


def _chain(logical: str, **kw):
    payload = dict(
        document_id=DOC,
        logical_channel=logical,
        channel_class='bed_channel',
        expected_speaker_entity_ids=(f'spk-{logical}',),
        declared_at_utc=_TS,
    )
    payload.update(kw)
    return build_channel_identity_chain(**payload)


def _plan(
    channels=('fl', 'fr'),
    *,
    reference=None,
    polarity=None,
    routings=None,
    **kw,
) -> ChannelVerificationPlan:
    chains = [_chain(ch) for ch in channels]
    routing_map = routings or {ch: _routing(i) for i, ch in enumerate(channels)}
    payload = dict(
        document_id=DOC,
        chains=chains,
        routings=routing_map,
        sample_rate_hz=SAMPLE_RATE,
        stimulus_start_hz=100.0,
        stimulus_end_hz=8000.0,
        stimulus_duration_s=0.05,
        stimulus_level_dbfs=-12.0,
        repetitions=2,
        reference_channel=reference,
        polarity_expectations=polarity,
        created_at_utc=_TS,
    )
    payload.update(kw)
    return build_verification_plan(**payload)


def _attestation(
    plan: ChannelVerificationPlan, logical: str,
    *, responded: bool = True,
) -> OperatorChannelAttestation:
    from htdt.canonical_json import canonical_sha256

    payload = {
        'document_id': DOC,
        'plan_ref': plan_binding(plan).model_dump(mode='json'),
        'logical_channel': logical,
        'attested_by': 'operator-1',
        'responded': responded,
        'attested_polarity': 'unknown',
        'note': None,
        'attested_at_utc': _TS2,
        'authority_version': '1',
    }
    sha = canonical_sha256(payload)
    return OperatorChannelAttestation(
        attestation_id='cvoa-' + sha[:24],
        document_id=DOC,
        plan_ref=plan_binding(plan),
        logical_channel=logical,
        attested_by='operator-1',
        responded=responded,
        attested_at_utc=_TS2,
        attestation_sha256=sha,
    )


def _arming(request: AcquisitionRequest) -> ArmConfirmation:
    r = request.routing
    return ArmConfirmation(
        acknowledged_playback_device_id=r.playback_device_id,
        acknowledged_playback_channel=r.playback_channel,
        acknowledged_capture_device_id=r.capture_device_id,
        acknowledged_capture_channel=r.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs,
    )


class _ScriptedChannelBackend(FakeAudioBackend):
    """Deterministic backend whose acoustic response differs per playback
    channel — stands in for distinct physical speakers. NOT the fake id:
    runs through it are physical-tier evidence in tests."""

    backend_id = 'scripted-audio-io'
    backend_version = 'scripted-1'

    def __init__(self, channel_scenarios: dict[int, dict]):
        super().__init__(default_fake_scenario())
        self._channel_scenarios = dict(channel_scenarios)

    def open_stream(self, config):
        channel = config.routing.playback_channel
        knobs = self._channel_scenarios.get(channel)
        if knobs is None:
            # No wired output on this channel: the simulated physical
            # topology cannot open the route at all.
            from htdt.cad_sweep_acquisition import (
                UnsupportedConfigurationError,
            )

            raise UnsupportedConfigurationError(
                f'no output wired on channel {channel}')
        self.scenario = default_fake_scenario(**knobs)
        return super().open_stream(config)


def _repo(tmp_path: Path) -> CadChannelVerificationRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadChannelVerificationRepository(SceneRepository(db))


# ---------------------------------------------------------------------------
# Plan construction + sealing


class TestPlan:
    def test_plan_binds_chains_and_routings(self) -> None:
        plan = _plan()
        assert plan.plan_id.startswith('cvpl-')
        assert len(plan.targets) == 2
        for target in plan.targets:
            assert target.chain_ref is not None
            assert target.chain_ref.ref_sha256 is not None
        assert plan.plan_sha256 == plan_binding(plan).ref_sha256

    def test_missing_routing_fails(self) -> None:
        with pytest.raises(ValueError, match='no routing bound'):
            _plan(routings={'fl': _routing(0)})

    def test_reference_channel_must_be_a_target(self) -> None:
        with pytest.raises(ValueError, match='reference_channel'):
            _plan(reference='sub', polarity={'fl': 'normal'})

    def test_polarity_expectation_requires_reference(self) -> None:
        with pytest.raises(ValueError, match='reference_channel'):
            _plan(polarity={'fl': 'normal'})

    def test_tampered_plan_rejected(self) -> None:
        plan = _plan()
        payload = plan.model_dump()
        payload['stimulus_level_dbfs'] = -30.0  # sha no longer matches
        with pytest.raises(ValidationError):
            ChannelVerificationPlan.model_validate(payload)


# ---------------------------------------------------------------------------
# End-to-end driver through the #869 engine


class TestDriver:
    def test_all_channels_verified_routing(self, tmp_path: Path) -> None:
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        verdict, results, engines = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        assert verdict.map_state == 'verified'
        for ch in ('fl', 'fr'):
            v = verdict.channel(ch)
            assert v is not None and v.routing_state == 'verified'
            assert v.evidence_class == 'machine_verified_routing'
            assert results[ch].acquisition_run_ref is not None
            assert engines[ch].stage == 'completed'

    def test_relative_polarity_verified_and_inverted(self) -> None:
        plan = _plan(
            ('fl', 'fr'),
            reference='fl',
            polarity={'fl': 'normal', 'fr': 'inverted'},
        )
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            # Wired out of polarity versus fl — expected, so consistent.
            1: {'measurement_ir_taps': ((0, -1.0),)},
        })
        verdict, _, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        assert verdict.map_state == 'verified'
        assert verdict.channel('fl').evidence_class == (
            'machine_verified_relative_polarity')
        assert verdict.channel('fr').polarity_state == 'consistent'

    def test_unexpected_inversion_flags_defect(self) -> None:
        plan = _plan(
            ('fl', 'fr'),
            reference='fl',
            polarity={'fl': 'normal', 'fr': 'normal'},
        )
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((0, -1.0),)},  # wired backwards
        })
        verdict, _, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        fr = verdict.channel('fr')
        assert fr.polarity_state == 'inverted'
        assert 'polarity_inverted' in fr.defect_flags
        assert verdict.map_state == 'failed'

    def test_missing_output_is_not_verified(self) -> None:
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            # channel 1 has no wired output at all
        })
        verdict, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        fr = verdict.channel('fr')
        assert fr.evidence_class == 'unknown'
        assert fr.routing_state == 'missing_output'
        assert 'response_missing' in fr.defect_flags
        assert 'capture_invalid' in fr.defect_flags
        assert verdict.map_state == 'failed'

    def test_duplicate_signature_blocks_verification(self) -> None:
        # Two logical channels physically bound to the same output — the
        # identical responses cannot both be machine-verified.
        plan = _plan(
            ('fl', 'fr'),
            routings={'fl': _routing(0), 'fr': _routing(0)},
        )
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
        })
        verdict, _, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        for ch in ('fl', 'fr'):
            v = verdict.channel(ch)
            assert v.routing_state == 'duplicate_suspect'
            assert 'duplicate_signature' in v.defect_flags
            assert v.evidence_class == 'machine_assisted_ambiguous'
        assert verdict.map_state == 'ambiguous'

    def test_level_mismatch_flagged(self) -> None:
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            # A much quieter physical output — arrival identical but the
            # level cannot be explained by the routing alone.
            1: {'measurement_ir_taps': ((0, 0.05),)},
        })
        verdict, _, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        fr = verdict.channel('fr')
        assert fr.routing_state == 'level_mismatch'
        assert 'level_mismatch' in fr.defect_flags

    def test_fake_backend_never_machine_verified(self) -> None:
        # The real fake backend reports simulated capture — test evidence
        # may describe behavior but must never reach a machine class.
        plan = _plan(('fl', 'fr'))
        backend = FakeAudioBackend(default_fake_scenario(
            measurement_ir_taps=((0, 1.0),)))
        verdict, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        assert all(r.backend_is_simulated for r in results.values())
        for ch in ('fl', 'fr'):
            v = verdict.channel(ch)
            assert 'simulated_backend' in v.defect_flags
            assert v.evidence_class != 'machine_verified_routing'
            assert v.evidence_class != 'machine_verified_relative_polarity'
        assert verdict.map_state != 'verified'

    def test_operator_attestation_fallback(self) -> None:
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            # channel 1 not wired
        })
        verdict, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        assert verdict.channel('fr').evidence_class == 'unknown'
        att = _attestation(plan, 'fr')
        verdict2 = evaluate_channel_verdicts(
            plan, results, {'fr': att}, evaluated_at_utc=_TS2)
        fr = verdict2.channel('fr')
        assert fr.evidence_class == 'operator_attested'
        assert verdict2.map_state != 'verified'

    def test_routing_change_stales_only_touched_channel(self) -> None:
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        _, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        # fr moved to a different physical output.
        current = {'fl': _routing(0), 'fr': _routing(3)}
        stale = stale_channels_for_routing(
            plan, results, current, SAMPLE_RATE)
        assert stale == ('fr',)

    def test_persists_sweep_run_records(self, tmp_path: Path) -> None:
        db = tmp_path / 'cad.sqlite3'
        ensure_native_schema(db)
        scene = SceneRepository(db)
        sweep_repo = CadSweepAcquisitionRepository(scene)
        plan = _plan(('fl', 'fr'))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        verdict, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            sweep_repository=sweep_repo, clock=lambda: _TS,
        )
        assert verdict.map_state == 'verified'
        # Each excitation pinned a real sealed #869 acquisition run.
        runs = sweep_repo.list_acquisition_runs(DOC)
        assert len(runs) == 2
        for ch in ('fl', 'fr'):
            ref = results[ch].acquisition_run_ref
            resolved = sweep_repo.get_acquisition_run(ref.ref_id)
            assert resolved is not None
            assert resolved.acquisition_sha256 == ref.ref_sha256


# ---------------------------------------------------------------------------
# Repository round-trip + append-only + tamper detection


class TestRepository:
    def _records(self, tmp_path: Path):
        plan = _plan()
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        verdict, results, _ = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: _TS,
        )
        return plan, verdict, results

    def test_plan_round_trip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        plan, _, _ = self._records(tmp_path)
        repo.save_plan(plan)
        assert repo.get_plan(plan.plan_id) == plan
        assert plan in repo.list_plans(DOC)

    def test_result_round_trip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        plan, _, results = self._records(tmp_path)
        repo.save_plan(plan)
        for result in results.values():
            repo.save_excitation_result(result)
        for result in results.values():
            assert repo.get_excitation_result(result.result_id) == result

    def test_verdict_round_trip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        plan, verdict, _ = self._records(tmp_path)
        repo.save_plan(plan)
        repo.save_verdict(verdict)
        assert repo.get_verdict(verdict.verdict_id) == verdict
        assert verdict in repo.list_verdicts(DOC)

    def test_append_only_conflict(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        plan, verdict, _ = self._records(tmp_path)
        repo.save_verdict(verdict)
        tampered = verdict.model_copy(update={'map_state': 'failed'})
        # id differs (sha changes) — craft same-id conflicting record
        tampered = verdict.model_copy(
            update={'evaluated_at_utc': _TS2,
                    'verdict_sha256': verdict.verdict_sha256,
                    'verdict_id': verdict.verdict_id})
        with pytest.raises(ChannelVerificationIntegrityError):
            repo.save_verdict(tampered)

    def test_row_tamper_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        plan, verdict, _ = self._records(tmp_path)
        repo.save_verdict(verdict)
        stored = repo.get_verdict(verdict.verdict_id)
        new_state = (
            'verified' if stored.map_state != 'verified' else 'failed')
        db = tmp_path / 'cad.sqlite3'
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE cad_channel_verification_verdicts "
                'SET map_state=? WHERE verdict_id=?',
                (new_state, verdict.verdict_id))
        with pytest.raises(ChannelVerificationIntegrityError):
            repo.get_verdict(verdict.verdict_id)


# ---------------------------------------------------------------------------
# Sealed-model hygiene


class TestSealing:
    def test_extra_fields_rejected(self) -> None:
        from htdt.cad_channel_verification import ChannelExcitationResult
        with pytest.raises(ValidationError):
            ChannelVerificationPlan(
                plan_id='cvpl-x', document_id=DOC, targets=(),
                sample_rate_hz=48000, stimulus_start_hz=100.0,
                stimulus_end_hz=8000.0, stimulus_duration_s=0.05,
                stimulus_level_dbfs=-12.0,
                created_at_utc=_TS, plan_sha256='0' * 64,
                unexpected='nope',
            )

    def test_response_signature_deterministic(self) -> None:
        a = response_signature(10, -20.0, 1)
        assert a == response_signature(10, -20.0, 1)
        assert a != response_signature(10, -20.0, -1)
        assert a != response_signature(11, -20.0, 1)

    def test_routing_signature_tracks_binding(self) -> None:
        a = routing_signature(_routing(0), SAMPLE_RATE)
        assert a == routing_signature(_routing(0), SAMPLE_RATE)
        assert a != routing_signature(_routing(1), SAMPLE_RATE)
        assert a != routing_signature(_routing(0), 44100)
