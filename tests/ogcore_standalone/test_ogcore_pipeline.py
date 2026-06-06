"""
OG-Core ingestion-layer verification harness.

Proves our ingestion layer produces identical results to standalone OG-Core,
by running the SAME parameters two ways and comparing the headline macro table:

    Path A, standalone OG-Core (the control): Specifications -> runner ->
            output_tables.macro_table, exactly as run_ogcore_example.py does.
    Path B, our ingestion layer (under test): OGCoreCase.create_run(params) ->
            OGCoreRunner.run -> OGCoreRunner.get_macro_table.

If A == B to floating-point precision, our layer adds zero distortion, meaning the
JSON parameter round-trip, the baseline/reform wiring, and the numpy->records
serialization are all faithful.

Two tiers, both opt-in (neither runs in the default `pytest` suite):

    pytest -m slow   reduced dimensions (OG-Core's own test config), minutes
    pytest -m gold   full dimensions, matched against the committed official
                     expected_ogcore_example_output.csv, hours

Both tiers run two live solves per path (the "two-solve" equivalence). Fixtures
are sourced from the pinned v.0.15.13 tag in tests/ogcore_standalone/ogcore_fixtures/.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from _ogcore_specs import EXPECTED_CSV, official_specs, reduced_specs

MACRO_VARS = ["Y", "C", "K", "L", "r", "w"]


# isolated_storage fixture is provided by conftest.py.


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _numeric_matrix(records: list) -> np.ndarray:
    """Numeric values from macro-table records (drops the 'Variable' label column)."""
    rows = []
    for rec in records:
        rows.append([float(v) for k, v in rec.items()
                     if k != "Variable" and isinstance(v, (int, float))])
    return np.array(rows, dtype=float)


def _run_standalone(base_spec: dict, reform_spec: dict, workdir: Path) -> list:
    """
    Path A: run OG-Core directly, like run_ogcore_example.py. Uses the same
    process-based client and worker count our layer uses, so A and B share an
    identical execution config and any divergence is then attributable to our
    layer.
    """
    from ogcore.parameters import Specifications
    from ogcore.execute import runner
    from ogcore import output_tables as ot
    from ogcore.utils import safe_read_pickle
    from distributed import Client
    from Classes.OGCore.OGCoreRunnerClass import _num_workers

    base_dir = str(workdir / "OUTPUT_BASELINE")
    reform_dir = str(workdir / "OUTPUT_REFORM")
    nw = _num_workers()

    with Client(n_workers=nw, threads_per_worker=1, dashboard_address=None) as client:
        pb = Specifications(baseline=True, num_workers=nw,
                            baseline_dir=base_dir, output_base=base_dir)
        pb.update_specifications(dict(base_spec))
        runner(pb, time_path=True, client=client)

        pr = Specifications(baseline=False, num_workers=nw,
                            baseline_dir=base_dir, output_base=reform_dir)
        pr.update_specifications(dict(reform_spec))
        runner(pr, time_path=True, client=client)

    base_tpi = safe_read_pickle(os.path.join(base_dir, "TPI", "TPI_vars.pkl"))
    base_p = safe_read_pickle(os.path.join(base_dir, "model_params.pkl"))
    reform_tpi = safe_read_pickle(os.path.join(reform_dir, "TPI", "TPI_vars.pkl"))
    reform_p = safe_read_pickle(os.path.join(reform_dir, "model_params.pkl"))

    df = ot.macro_table(
        base_tpi, base_p, reform_tpi=reform_tpi, reform_params=reform_p,
        var_list=MACRO_VARS, output_type="pct_diff",
        num_years=10, start_year=int(base_p.start_year), include_SS=True,
    )
    return df.to_dict(orient="records")


def _run_via_layer(base_spec: dict, reform_spec: dict) -> list:
    """Path B: drive the same computation through our ingestion layer."""
    from Classes.OGCore.OGCoreClass import OGCoreCase
    from Classes.OGCore.OGCoreRunnerClass import OGCoreRunner

    case = OGCoreCase("verif")
    case.create_case({"ogc-casename": "verif", "ogc-desc": "verification"})

    case.create_run("Base", "baseline", None, params=dict(base_spec))
    rb = OGCoreRunner("verif").run("Base", time_path=True)
    assert rb["status_code"] == "success", f"baseline failed: {rb}"

    case.create_run("Ref", "reform", "Base", params=dict(reform_spec))
    rr = OGCoreRunner("verif").run("Ref", time_path=True)
    assert rr["status_code"] == "success", f"reform failed: {rr}"

    return OGCoreRunner("verif").get_macro_table(
        "Base", reform_run="Ref",
        var_list=MACRO_VARS, output_type="pct_diff", num_years=10, include_SS=True,
    )


# Reduced / official og_spec builders live in _ogcore_specs.py in this folder
# (shared with the production-context test).


# ──────────────────────────────────────────────────────────────────────────
# Tier 1, REDUCED: equivalence proof (Path A == Path B)
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_pipeline_equivalence_reduced(isolated_storage, tmp_path):
    """
    Our ingestion layer must produce the same macro table as standalone
    OG-Core for identical inputs. Both paths run two live solves (baseline +
    reform) at reduced dimensions. Tight tolerance: both run the identical
    deterministic computation; the only difference is B's JSON parameter
    round-trip, which is lossless for these values.
    """
    base_spec, reform_spec = reduced_specs()

    records_A = _run_standalone(base_spec, reform_spec, tmp_path / "standalone")
    records_B = _run_via_layer(base_spec, reform_spec)

    mat_A = _numeric_matrix(records_A)
    mat_B = _numeric_matrix(records_B)

    assert mat_A.shape == mat_B.shape, (mat_A.shape, mat_B.shape)
    assert np.allclose(mat_A, mat_B, atol=1e-8, rtol=1e-8), (
        "Ingestion layer diverges from standalone OG-Core.\n"
        f"max abs diff = {np.abs(mat_A - mat_B).max()}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Tier 2, GOLD: full-dimension match vs official expected CSV
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.gold
def test_pipeline_matches_official_gold(isolated_storage):
    """
    Drive the official example parameters (full S=80, T=320) through our layer
    and match the committed expected_ogcore_example_output.csv from the pinned
    v.0.15.13 tag. Tolerance is looser than the equivalence test: the reference
    CSV was generated with a multi-worker Dask client and is stored rounded,
    whereas we run serially, so the equilibrium is the same but the last digits
    differ.
    """
    base_spec, reform_spec = official_specs()

    records_B = _run_via_layer(base_spec, reform_spec)
    mat_B = _numeric_matrix(records_B)

    expected = pd.read_csv(EXPECTED_CSV)
    # Drop the leading index column and the 'Variable' label column.
    numeric_cols = [c for c in expected.columns
                    if c not in ("Variable",) and not c.startswith("Unnamed")]
    mat_expected = expected[numeric_cols].to_numpy(dtype=float)

    assert mat_B.shape == mat_expected.shape, (mat_B.shape, mat_expected.shape)
    assert np.allclose(mat_B, mat_expected, atol=1e-3, rtol=1e-2), (
        "Full-dimension output does not match the official expected CSV.\n"
        f"max abs diff = {np.abs(mat_B - mat_expected).max()}"
    )
