"""Round-14 equipment/spec-library data-truth regression tests.

Covers the shipped catalog reachability + scope labeling, unit/range
bounds on declared spec fields, cross-field contradictions, picker label
disambiguation, and user-entry validation.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from htdt.cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    ElectricalValue,
    FrequencyDomain,
    build_amplifier_output_capability,
)
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    SensitivityReference,
    SplCapability,
)
from htdt.cad_material_library import (
    BUILTIN_MATERIAL_IDS,
    build_material_evidence,
)
from htdt.cad_material_library_repository import CadMaterialLibraryRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_speaker_library import BUILTIN_SPEAKER_IDS
from htdt.cad_speaker_library_repository import CadSpeakerLibraryRepository
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.equipment_library import EquipmentLibraryService
from htdt.reference_libraries import LibraryFamily, LibraryScope
from htdt.reference_library_sources import build_reference_library_index
from htdt.system_expansion_workflow import SystemExpansionWorkflowService

NOW = "2026-09-28T00:00:00+00:00"


def _provenance(
    evidence_kind: str = "user_defined",
    source_name: str = "fixture-source",
) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version="1.0",
        source_reference="fixture-section",
        source_sha256="a" * 64,
    )


def _domain() -> FrequencyDomain:
    return FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)


# --- shipped catalog reachability + scope truth -------------------------


def test_index_seeds_builtin_catalogs_with_builtin_scope(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    index = build_reference_library_index(scenes, tmp_path / "data")

    equipment = index.entries(family=LibraryFamily.EQUIPMENT)
    builtin_speakers = [
        e for e in equipment if e.identity in BUILTIN_SPEAKER_IDS
    ]
    assert len(builtin_speakers) == len(BUILTIN_SPEAKER_IDS)
    assert all(e.scope == LibraryScope.BUILTIN for e in builtin_speakers)

    materials = index.entries(family=LibraryFamily.MATERIAL)
    builtin_materials = [
        e for e in materials if e.identity in BUILTIN_MATERIAL_IDS
    ]
    assert len(builtin_materials) == len(BUILTIN_MATERIAL_IDS)
    assert all(e.scope == LibraryScope.BUILTIN for e in builtin_materials)

    # Bundled projector / tactile / screen references are also discoverable.
    display_names = {e.display_name for e in index.entries()}
    assert any("DLA-NZ500" in name for name in display_names)
    assert any("ButtKicker" in name for name in display_names)
    assert any("Harmony G3" in name for name in display_names)


def test_builtin_seed_is_idempotent(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    speakers = CadSpeakerLibraryRepository(scenes)
    materials = CadMaterialLibraryRepository(scenes)
    speakers.install_builtin_library()
    materials.install_builtin_library()
    speakers.install_builtin_library()
    materials.install_builtin_library()
    assert len(speakers.list_speakers()) == len(BUILTIN_SPEAKER_IDS)
    assert len(materials.list_materials()) == len(BUILTIN_MATERIAL_IDS)


# --- material evidence bounds -------------------------------------------


def _evidence(**overrides):
    kwargs = {
        "material_id": "mat-1",
        "quantity": "random_incidence_absorption_coefficient",
        "frequency_hz": (125.0, 250.0),
        "values": (0.3, 0.7),
        "unit_label": "-",
        "provenance_class": "laboratory_measured",
        "created_at_utc": NOW,
    }
    kwargs.update(overrides)
    return build_material_evidence(**kwargs)


def test_absorption_coefficient_bounded_to_unit_interval() -> None:
    with pytest.raises(ValueError):
        _evidence(values=(0.3, 1.5))
    with pytest.raises(ValueError):
        _evidence(
            quantity="scattering_coefficient",
            values=(0.2, -0.1),
            unit_label="-",
        )
    _evidence(values=(0.0, 1.0))


def test_transmission_loss_non_negative() -> None:
    with pytest.raises(ValueError):
        _evidence(
            quantity="transmission_loss",
            values=(20.0, -3.0),
            unit_label="dB",
        )
    _evidence(quantity="transmission_loss", values=(20.0, 0.0), unit_label="dB")


def test_incidence_angle_requires_oblique_incidence() -> None:
    with pytest.raises(ValueError):
        _evidence(incidence="unknown", incidence_angle_deg=45.0)
    with pytest.raises(ValueError):
        _evidence(incidence="random", incidence_angle_deg=45.0)
    _evidence(incidence="oblique", incidence_angle_deg=45.0)


# --- equipment spec-field bounds ----------------------------------------


def test_sensitivity_level_rejects_wrong_unit_and_absurd_values() -> None:
    for bad in (0.087, -5.0, 0.0, 201.0):
        with pytest.raises(ValueError):
            SensitivityReference(
                level_db_spl=bad,
                input_quantity="voltage_v_rms",
                input_value=2.83,
                distance_m=1.0,
                provenance=_provenance(),
            )
    SensitivityReference(
        level_db_spl=87.0,
        input_quantity="power_w",
        input_value=1.0,
        distance_m=1.0,
        provenance=_provenance(),
    )


def test_spl_capability_bounds_and_headroom_consistency() -> None:
    with pytest.raises(ValueError):
        SplCapability(
            continuous_db_spl=240.0,
            reference_distance_m=1.0,
            provenance=_provenance(),
        )
    with pytest.raises(ValueError):
        # Headroom reference above the declared maximum capability.
        SplCapability(
            continuous_db_spl=103.0,
            reference_distance_m=1.0,
            declared_headroom_db=6.0,
            headroom_reference_level_db_spl=120.0,
            provenance=_provenance(),
        )
    # A declared headroom pair without continuous/peak stays valid.
    SplCapability(
        reference_distance_m=1.0,
        declared_headroom_db=6.0,
        headroom_reference_level_db_spl=105.0,
        provenance=_provenance(),
    )
    SplCapability(
        continuous_db_spl=103.0,
        peak_db_spl=112.0,
        reference_distance_m=1.0,
        declared_headroom_db=9.0,
        headroom_reference_level_db_spl=103.0,
        provenance=_provenance(),
    )


# --- amplifier capability cross-field truth ------------------------------


def _amplifier_kwargs(**overrides):
    kwargs = {
        "capability_id": "amp-1",
        "version": "1",
        "identity_kind": "user_defined",
        "user_label": "fixture amp",
        "output_id": "front-l",
        "provenance": (_provenance(),),
        "supported_load": AmplifierLoadDomain(
            reference_load_ohm=4.0,
            minimum_load_ohm=4.0,
            maximum_load_ohm=16.0,
        ),
        "clipping_reference_definition": "rated sine clip",
        "valid_frequency_band": _domain(),
        "weighting": "none",
        "channel_count_condition": AmplifierChannelCountCondition(
            simultaneous_channel_count=1,
            condition_description="single channel",
        ),
        "continuous_capability": ElectricalValue(
            quantity="voltage_v_rms", value=40.0
        ),
        "continuous_duration_s": 60.0,
        "peak_capability": ElectricalValue(
            quantity="voltage_v_rms", value=50.0
        ),
        "peak_duration_s": 0.05,
    }
    kwargs.update(overrides)
    return kwargs


def test_amplifier_peak_below_continuous_rejected() -> None:
    with pytest.raises(ValueError):
        build_amplifier_output_capability(
            **_amplifier_kwargs(
                peak_capability=ElectricalValue(
                    quantity="voltage_v_rms", value=30.0
                )
            )
        )
    # Different quantities cannot be ordered against each other.
    build_amplifier_output_capability(
        **_amplifier_kwargs(
            peak_capability=ElectricalValue(quantity="power_w", value=30.0)
        )
    )
    build_amplifier_output_capability(**_amplifier_kwargs())


# --- user entries: validation + picker identity --------------------------


def _service(scenes: SceneRepository) -> EquipmentLibraryService:
    return EquipmentLibraryService(
        scenes, CadSystemVariantRepository(scenes)
    )


def test_user_definition_requires_nonblank_label_and_sources(
    tmp_path: Path,
) -> None:
    service = _service(SceneRepository(tmp_path / "cad.sqlite3"))
    base = dict(
        manufacturer=None,
        model=None,
        width_m=0.2,
        height_m=0.3,
        depth_m=0.3,
        evidence_kind="user_defined",
        actor="test",
    )
    with pytest.raises(ValueError):
        service.create_user_definition(
            user_label="   ",
            source_name="n",
            source_version="v",
            source_reference="r",
            **base,
        )
    with pytest.raises(ValueError):
        service.create_user_definition(
            user_label="ok",
            source_name="   ",
            source_version="v",
            source_reference="r",
            **base,
        )
    definition = service.create_user_definition(
        user_label="  My Speaker  ",
        source_name="n",
        source_version="v",
        source_reference="r",
        **base,
    )
    assert definition.user_label == "My Speaker"


def test_equipment_choices_disambiguate_versions(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    service = _service(scenes)
    base = service.create_user_definition(
        user_label="Fixture",
        manufacturer=None,
        model=None,
        width_m=0.2,
        height_m=0.3,
        depth_m=0.3,
        evidence_kind="user_defined",
        source_name="n",
        source_version="v",
        source_reference="r",
        actor="test",
    )
    service.create_next_version(
        base,
        version="1-u2",
        user_label="Fixture",
        manufacturer=None,
        model=None,
        width_m=0.2,
        height_m=0.3,
        depth_m=0.3,
        evidence_kind="user_defined",
        source_name="n",
        source_version="v",
        source_reference="r",
        actor="test",
    )
    expansion = SystemExpansionWorkflowService(scenes, "doc-1")
    labels = [label for _sha, label in expansion.equipment_choices()]
    assert len(labels) == 2
    assert len(set(labels)) == 2
    assert all("(v" in label for label in labels)
