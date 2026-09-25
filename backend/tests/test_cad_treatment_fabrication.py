from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_acoustic_treatment import (
    TreatmentDimensions,
    TreatmentLayer,
    TreatmentProvenance,
    build_acoustic_treatment_definition,
)
from htdt.cad_treatment_fabrication import (
    FabricationSpec,
    FabricationToleranceProfile,
    generate_panel_fabrication,
    generate_qrd_fabrication,
    qrd_depth_table,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

NOW = '2026-09-24T00:00:00+00:00'


def _provenance() -> TreatmentProvenance:
    return TreatmentProvenance(
        source_kind='user_defined',
        source_id='user-1',
        source_version='1',
        source_authority=ExactExternalAuthorityRef(
            authority_id='user-treatment-authority',
            authority_version='1',
            semantic_hash_sha256='d' * 64,
        ),
    )


def _panel_definition():
    return build_acoustic_treatment_definition(
        definition_id='treat-1',
        version='1',
        name='100mm porous panel + 50mm gap',
        treatment_type='absorber_with_air_gap',
        provenance=_provenance(),
        dimensions=TreatmentDimensions(
            width_m=0.6, height_m=1.2, thickness_m=0.1
        ),
        air_gap_m=0.05,
        layers=(
            TreatmentLayer(
                layer_id='wool',
                material_name='mineral wool',
                thickness_m=0.1,
                airflow_resistivity_pa_s_m2=12000.0,
            ),
        ),
    )


def _diffuser_definition():
    return build_acoustic_treatment_definition(
        definition_id='treat-qrd',
        version='1',
        name='N7 QRD',
        treatment_type='diffuser_scattering_element',
        provenance=_provenance(),
        dimensions=TreatmentDimensions(
            width_m=0.612, height_m=1.2, thickness_m=0.2
        ),
        layers=(),
    )


def test_fab10_exact_dimensions_and_cut_list() -> None:
    package = generate_panel_fabrication(
        _panel_definition(),
        FabricationSpec(spec_version='1', panel_count=6),
        created_at_utc=NOW,
    )
    assert package.fabrication_family == 'rectangular_panel'
    assert package.overall_width_m == pytest.approx(0.6)
    assert package.overall_height_m == pytest.approx(1.2)
    # thickness 0.1 + air gap 0.05
    assert package.overall_depth_m == pytest.approx(0.15)
    assert len(package.cut_list) == 1
    entry = package.cut_list[0]
    assert entry.quantity == 6
    assert entry.dimensions_m == (0.6, 1.2, 0.1)
    assert any('air gap' in w for w in package.warnings)
    # package binds the exact definition hash
    assert package.definition_sha256 == _panel_definition().definition_sha256
    assembly = [b for b in package.bom_fragments if b.line_kind == 'assembly']
    assert assembly[0].quantity == 6


def test_fab10_rejects_wrong_family() -> None:
    with pytest.raises(ValueError, match='FAB10'):
        generate_panel_fabrication(
            _diffuser_definition(),
            FabricationSpec(spec_version='1'),
            created_at_utc=NOW,
        )


def test_qrd_sequence_is_exact_for_n7() -> None:
    wells = qrd_depth_table(7, 500.0, 343.0, 1)
    residues = [row.residue for row in wells]
    assert residues == [0, 1, 4, 2, 2, 4, 1]
    wavelength = 343.0 / 500.0
    depths = [row.depth_m for row in wells]
    assert depths[2] == pytest.approx(4.0 * wavelength / 14.0)
    assert max(depths) == pytest.approx(4.0 * wavelength / 14.0)


def test_qrd_requires_odd_prime() -> None:
    with pytest.raises(ValueError, match='odd prime'):
        qrd_depth_table(4, 500.0, 343.0, 1)
    with pytest.raises(ValueError, match='odd prime'):
        qrd_depth_table(9, 500.0, 343.0, 1)


def test_fab20_package_parts_and_cut_groups() -> None:
    package = generate_qrd_fabrication(
        _diffuser_definition(),
        FabricationSpec(
            spec_version='1',
            panel_count=2,
            material_ref='18mm birch ply',
            tolerances=FabricationToleranceProfile(well_depth_mm=1.0),
        ),
        qrd_prime=7,
        design_frequency_hz=500.0,
        well_width_m=0.08,
        fin_thickness_m=0.004,
        periods=1,
        back_thickness_m=0.018,
        created_at_utc=NOW,
    )
    assert package.fabrication_family == 'qrd_1d'
    assert len(package.well_table) == 7
    # 7 wells + 8 fins; width = 7*0.08 + 8*0.004
    assert package.overall_width_m == pytest.approx(0.592)
    assert any(w.cut_group.startswith('fin-') for w in package.cut_list)
    fin_entries = [
        e for e in package.cut_list if e.cut_group.startswith('fin-')
    ]
    assert all(e.tolerance_mm == 1.0 for e in fin_entries)
    fin_total = sum(e.quantity for e in fin_entries)
    # every fin position with nonzero depth, doubled for 2 panels;
    # depth-0 wells at index 0 don't change fin depth (max of neighbors)
    backing = [p for p in package.parts if p.part_kind == 'backing_panel']
    assert backing[0].finished_depth_m == pytest.approx(0.018)
    assert fin_total > 0
    # supersede chain: a new package may point at an issued one
    successor = generate_qrd_fabrication(
        _diffuser_definition(),
        FabricationSpec(
            spec_version='2',
            panel_count=2,
            supersedes_package_sha256=package.package_sha256,
        ),
        qrd_prime=7,
        design_frequency_hz=500.0,
        well_width_m=0.08,
        fin_thickness_m=0.004,
        periods=1,
        back_thickness_m=0.018,
        created_at_utc=NOW,
        package_version='2',
    )
    assert successor.supersedes_package_sha256 == package.package_sha256
    assert successor.package_sha256 != package.package_sha256


def test_fab20_rejects_non_diffuser() -> None:
    with pytest.raises(ValueError, match='FAB20'):
        generate_qrd_fabrication(
            _panel_definition(),
            FabricationSpec(spec_version='1'),
            qrd_prime=7,
            design_frequency_hz=500.0,
            well_width_m=0.08,
            fin_thickness_m=0.004,
            periods=1,
            back_thickness_m=0.018,
            created_at_utc=NOW,
        )


def test_package_is_immutable() -> None:
    package = generate_panel_fabrication(
        _panel_definition(),
        FabricationSpec(spec_version='1'),
        created_at_utc=NOW,
    )
    with pytest.raises(ValidationError):
        package.panel_count = 99
