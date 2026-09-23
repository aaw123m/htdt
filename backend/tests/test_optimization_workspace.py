from __future__ import annotations

import numpy as np

from htdt.cad_search_models import CadCandidate, CadCandidateSetPage
from htdt.optimization_workspace import candidate_cloud_points


def test_candidate_cloud_uses_one_primary_entity_point_per_visible_candidate() -> None:
    page = CadCandidateSetPage(
        search_spec_id='spec-1',
        search_spec_sha256='a' * 64,
        candidate_set_sha256='b' * 64,
        raw_candidate_count=3,
        feasible_candidate_count=2,
        rejected_candidate_count=1,
        duplicate_candidate_count=0,
        rejection_counts={'rack': 1},
        offset=0,
        limit=250,
        candidates=(
            CadCandidate(
                candidate_id='candidate-1',
                raw_index=0,
                feasible_index=0,
                positions={
                    'speaker-fl': {'x_m': 1.0, 'y_m': 1.5, 'z_m': 1.0},
                    'speaker-fr': {'x_m': 5.0, 'y_m': 1.5, 'z_m': 1.0},
                },
            ),
            CadCandidate(
                candidate_id='candidate-2',
                raw_index=2,
                feasible_index=1,
                positions={
                    'speaker-fl': {'x_m': 1.2, 'y_m': 1.7, 'z_m': 1.0},
                    'speaker-fr': {'x_m': 4.8, 'y_m': 1.7, 'z_m': 1.0},
                },
            ),
        ),
    )

    points = candidate_cloud_points(page, 'speaker-fl')

    assert points.shape == (2, 3)
    assert np.allclose(points, np.asarray(((1.0, 1.5, 1.0), (1.2, 1.7, 1.0))))
