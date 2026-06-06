"""
OG-Core safety guards: Option B (reform TPI needs a TPI-complete baseline) and
structural compatibility (a reform must share model dimensions with its baseline).

Both are exercised WITHOUT solving the model. The Option-B rejection path returns
before OG-Core is imported, so it is fast; the structural comparison is unit
tested directly. These guard against a regression that would let an invalid
reform reach the solver and crash (or, worse, produce silent garbage).
"""

from types import SimpleNamespace

from Classes.OGCore.OGCoreClass import OGCoreCase
from Classes.OGCore.OGCoreRunnerClass import OGCoreRunner, _STRUCTURAL_FIELDS


# ── Option B: reform TPI requires a baseline run with time_path=True ────────

def test_reform_tpi_on_ss_only_baseline_is_rejected(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    # baseline completed but SS-only (no transition path)
    case.update_run_status("Base", "completed", time_path=False)
    case.create_run("Ref", "reform", "Base", params={})

    resp = OGCoreRunner("Kenya").run("Ref", time_path=True)

    assert resp["status_code"] == "error"
    assert "time_path" in resp["message"]
    # the run must remain pending (it was rejected before any execution)
    assert case.get_run_meta("Ref")["status"] == "pending"


# ── Structural compatibility (unit) ─────────────────────────────────────────

def test_structural_mismatch_detects_each_dimension():
    runner = OGCoreRunner("x")
    base = SimpleNamespace(S=80, T=320, J=7, M=1, I=1, start_year=2021)
    for field in _STRUCTURAL_FIELDS:
        p = SimpleNamespace(S=80, T=320, J=7, M=1, I=1, start_year=2021)
        setattr(p, field, int(getattr(base, field)) + 1)  # diverge exactly one
        mism = runner._structural_mismatches(p, base)
        assert mism, f"{field} mismatch not detected"
        assert any(field in m for m in mism), (field, mism)


def test_structural_match_has_no_mismatch():
    runner = OGCoreRunner("x")
    p = SimpleNamespace(S=80, T=320, J=7, M=1, I=1, start_year=2021)
    base = SimpleNamespace(S=80, T=320, J=7, M=1, I=1, start_year=2021)
    assert runner._structural_mismatches(p, base) == []


def test_structural_fields_are_the_expected_set():
    # If OG-Core's dimension set changes, this should be revisited deliberately.
    assert _STRUCTURAL_FIELDS == ("S", "T", "J", "M", "I", "start_year")
