"""
Tests for the list-mutation-during-iteration fixes in DataFileClass.

Validates that deleteScenarioCaseRuns and deleteCaseRun correctly
remove *all* matching entries, not just the first one — the old code
called list.remove() inside a for-loop, which skips elements when
the list shrinks mid-iteration.

These tests are self-contained and do not require Flask or the app.
They test the pure-Python logic by constructing the DataFile object
with mocked filesystem paths.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the API directory is on sys.path so we can import the classes
API_DIR = str(Path(__file__).resolve().parent.parent / "API")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_case_tree(tmp: Path, case: str, res_data: dict, gen_data: dict):
    """Create the minimal directory/file tree that DataFile.__init__ expects."""
    case_dir = tmp / case
    view_dir = case_dir / "view"
    res_dir = case_dir / "res"
    view_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    # genData.json
    (case_dir / "genData.json").write_text(json.dumps(gen_data), encoding="utf-8")

    # resData.json (lives inside view/)
    (view_dir / "resData.json").write_text(json.dumps(res_data), encoding="utf-8")

    # Parameters.json & Variables.json at DataStorage root
    if not (tmp / "Parameters.json").exists():
        (tmp / "Parameters.json").write_text(json.dumps({}), encoding="utf-8")
    if not (tmp / "Variables.json").exists():
        (tmp / "Variables.json").write_text(json.dumps({}), encoding="utf-8")


# Minimal genData that the Osemosys constructor won't choke on
MINIMAL_GEN_DATA = {
    "osy-years": ["2020", "2025"],
    "osy-tech": [{"TechId": "T1", "Tech": "Tech1"}],
    "osy-comm": [{"CommId": "C1", "Comm": "Comm1"}],
    "osy-emis": [{"EmisId": "E1", "Emis": "Emis1"}],
    "osy-stg": [],
    "osy-ts": [{"TsId": "TS1", "Ts": "Ts1"}],
    "osy-se": [{"SeId": "SE1", "Se": "Se1"}],
    "osy-dt": [{"DtId": "DT1", "Dt": "Dt1"}],
    "osy-dtb": [{"DtbId": "DTB1", "Dtb": "Dtb1"}],
    "osy-constraints": [],
    "osy-scenarios": [],
    "osy-mo": "1",
}


def _build_datafile(tmp_path, case, res_data):
    """Build a DataFile instance pointed at a tmp_path case directory."""
    _make_case_tree(tmp_path, case, res_data, MINIMAL_GEN_DATA)

    with patch("Classes.Base.Config.DATA_STORAGE", str(tmp_path)):
        from Classes.Case.DataFileClass import DataFile
        df = DataFile(case)
    return df


# ---------------------------------------------------------------------------
# deleteScenarioCaseRuns
# ---------------------------------------------------------------------------


class TestDeleteScenarioCaseRuns:
    """Verify that deleteScenarioCaseRuns removes *all* matching scenarios."""

    def test_removes_single_scenario(self, tmp_path):
        """Basic case: one matching scenario across one case run."""
        res_data = {
            "osy-cases": [
                {
                    "Case": "run1",
                    "Scenarios": [
                        {"ScenarioId": "SC_A", "Scenario": "A", "Active": True},
                        {"ScenarioId": "SC_B", "Scenario": "B", "Active": True},
                    ],
                }
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        resp = df.deleteScenarioCaseRuns("SC_A")

        assert resp["status_code"] == "success"
        remaining_ids = [
            sc["ScenarioId"] for sc in df.resData["osy-cases"][0]["Scenarios"]
        ]
        assert "SC_A" not in remaining_ids
        assert "SC_B" in remaining_ids

    def test_removes_consecutive_duplicates(self, tmp_path):
        """
        Regression: the old code skipped a scenario when two consecutive
        entries matched because list.remove() shifted elements mid-iteration.
        """
        res_data = {
            "osy-cases": [
                {
                    "Case": "run1",
                    "Scenarios": [
                        {"ScenarioId": "SC_X", "Scenario": "X1", "Active": True},
                        {"ScenarioId": "SC_X", "Scenario": "X2", "Active": False},
                        {"ScenarioId": "SC_Y", "Scenario": "Y", "Active": True},
                    ],
                }
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        df.deleteScenarioCaseRuns("SC_X")

        remaining_ids = [
            sc["ScenarioId"] for sc in df.resData["osy-cases"][0]["Scenarios"]
        ]
        # Both SC_X entries must be gone
        assert remaining_ids == ["SC_Y"]

    def test_removes_across_multiple_case_runs(self, tmp_path):
        """Scenario should be removed from every case run, not just the first."""
        res_data = {
            "osy-cases": [
                {
                    "Case": "run1",
                    "Scenarios": [
                        {"ScenarioId": "SC_A", "Scenario": "A", "Active": True},
                    ],
                },
                {
                    "Case": "run2",
                    "Scenarios": [
                        {"ScenarioId": "SC_A", "Scenario": "A", "Active": True},
                        {"ScenarioId": "SC_B", "Scenario": "B", "Active": True},
                    ],
                },
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        df.deleteScenarioCaseRuns("SC_A")

        for case in df.resData["osy-cases"]:
            ids = [sc["ScenarioId"] for sc in case["Scenarios"]]
            assert "SC_A" not in ids

    def test_no_match_is_noop(self, tmp_path):
        """Deleting a non-existent scenario should succeed without changing data."""
        res_data = {
            "osy-cases": [
                {
                    "Case": "run1",
                    "Scenarios": [
                        {"ScenarioId": "SC_A", "Scenario": "A", "Active": True},
                    ],
                }
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        df.deleteScenarioCaseRuns("SC_NONEXISTENT")

        remaining_ids = [
            sc["ScenarioId"] for sc in df.resData["osy-cases"][0]["Scenarios"]
        ]
        assert remaining_ids == ["SC_A"]


# ---------------------------------------------------------------------------
# deleteCaseRun
# ---------------------------------------------------------------------------


class TestDeleteCaseRun:
    """Verify that deleteCaseRun removes the matching case run entry."""

    def test_results_only_does_not_touch_osy_cases(self, tmp_path):
        res_data = {
            "osy-cases": [
                {"Case": "run1", "Scenarios": []},
                {"Case": "run2", "Scenarios": []},
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        resp = df.deleteCaseRun("run1", resultsOnly=True)

        # resultsOnly=True means the osy-cases list should NOT be modified
        case_names = [c["Case"] for c in df.resData["osy-cases"]]
        assert "run1" in case_names

    def test_removes_case_from_resdata_when_not_results_only(self, tmp_path):
        res_data = {
            "osy-cases": [
                {"Case": "run1", "Scenarios": []},
                {"Case": "run2", "Scenarios": []},
            ]
        }

        df = _build_datafile(tmp_path, "model", res_data)
        df.deleteCaseRun("run1", resultsOnly=False)

        case_names = [c["Case"] for c in df.resData["osy-cases"]]
        assert "run1" not in case_names
        assert "run2" in case_names
