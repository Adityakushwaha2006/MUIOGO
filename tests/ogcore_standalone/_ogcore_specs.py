"""
Shared OG-Core parameter specs for the pipeline / production / parity tests.

Not a test module (no ``test_`` prefix) so pytest does not collect it. Kept on
the path via ``pythonpath = ["API", "tests"]`` in pyproject.toml.
"""

import json
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).parent / "ogcore_fixtures"
TESTING_PARAMS = FIXTURES / "testing_params.json"
EXPECTED_CSV = FIXTURES / "expected_ogcore_example_output.csv"


def reduced_specs() -> tuple[dict, dict]:
    """
    Baseline + reform og_spec for the reduced tier.

    Both halves come from official sources. testing_params.json supplies the
    reduced dimensional arrays (S=40, T=120, J=2); you cannot just set S=40 on the
    full spec, because OG-Core's default demographic/ability arrays are sized for
    S=80 and do not all auto-interpolate. On top of that we overlay the official
    example's fiscal calibration (alpha_T, alpha_G, debt_ratio_ss=1.0), since
    testing_params' bare fiscal closure drives debt into its ceiling and TPI fails
    to converge. Verified to converge as a standalone baseline+reform at roughly
    3 min/solve.
    """
    with open(TESTING_PARAMS) as f:
        base_spec = json.load(f)
    alpha_T = np.zeros(50)
    alpha_T[0:2] = 0.09
    alpha_T[2:10] = 0.09 + 0.01
    alpha_T[10:40] = 0.09 - 0.01
    alpha_T[40:] = 0.09
    alpha_G = np.zeros(7)
    alpha_G[0:3] = 0.05 - 0.01
    alpha_G[3:6] = 0.05 - 0.005
    alpha_G[6:] = 0.05
    base_spec.update({
        "frisch": 0.41, "start_year": 2021, "cit_rate": [[0.21]], "debt_ratio_ss": 1.0,
        "alpha_T": alpha_T.tolist(), "alpha_G": alpha_G.tolist(), "initial_guess_r_SS": 0.04,
    })
    reform_spec = {**base_spec, "cit_rate": [[0.35]]}
    return base_spec, reform_spec


def official_specs() -> tuple[dict, dict]:
    """Baseline + reform og_spec for the gold tier (the official example params)."""
    alpha_T = np.zeros(50)
    alpha_T[0:2] = 0.09
    alpha_T[2:10] = 0.09 + 0.01
    alpha_T[10:40] = 0.09 - 0.01
    alpha_T[40:] = 0.09
    alpha_G = np.zeros(7)
    alpha_G[0:3] = 0.05 - 0.01
    alpha_G[3:6] = 0.05 - 0.005
    alpha_G[6:] = 0.05
    base_spec = {
        "frisch": 0.41,
        "start_year": 2021,
        "cit_rate": [[0.21]],
        "debt_ratio_ss": 1.0,
        "alpha_T": alpha_T.tolist(),
        "alpha_G": alpha_G.tolist(),
        "initial_guess_r_SS": 0.04,
    }
    reform_spec = {**base_spec, "cit_rate": [[0.35]]}
    return base_spec, reform_spec
