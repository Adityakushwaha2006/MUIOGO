"""
OG-Core case + run lifecycle (CRUD).

The happy-path coverage CLEWS does not have: create a case, add runs, enforce
the baseline-before-reform rule, and tear it all down, checking the on-disk
state (genData.json, run_meta.json, the run index) at each step. Class-level
tests assert disk structure directly; a couple of HTTP tests confirm the routes
wire through to the same behaviour. Fast, no model solves.
"""

from Classes.Base.FileClass import File
from Classes.OGCore.OGCoreClass import OGCoreCase


# ── Case CRUD (class level) ─────────────────────────────────────────────────

def test_create_case_writes_expected_gendata(isolated_storage):
    case = OGCoreCase("Kenya")
    resp = case.create_case({"ogc-casename": "Kenya", "ogc-desc": "demo"})
    assert resp["status_code"] == "created"

    gd = File.readFile(case.gen_data_path)
    assert gd["ogc-casename"] == "Kenya"
    assert gd["ogc-desc"] == "demo"
    assert gd["ogc-runs"] == []
    assert gd["ogc-version"] == "1.0"
    assert case.res_path.is_dir()
    # params are per-run, so no case-level params file
    assert not (case.case_path / "ogcParams.json").exists()


def test_save_case_preserves_runs(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    assert len(case.gen_data["ogc-runs"]) == 1

    # editing the case metadata must not wipe the run index
    resp = case.save_case({"ogc-casename": "Kenya", "ogc-desc": "updated"})
    assert resp["status_code"] == "edited"
    assert len(OGCoreCase("Kenya").gen_data["ogc-runs"]) == 1


def test_delete_case_removes_directory(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    assert case.case_path.exists()
    case.delete_case()
    assert not case.case_path.exists()


# ── Run creation + the baseline/reform rule ─────────────────────────────────

def test_create_baseline_run_structure(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    resp = case.create_run("Base", "baseline", None, params={})
    assert resp["status_code"] == "success"

    run_dir = case.res_path / "Base"
    assert (run_dir / "SS").is_dir()
    assert (run_dir / "TPI").is_dir()
    assert (run_dir / "ogcParams.json").is_file()

    meta = File.readFile(run_dir / "run_meta.json")
    assert meta["run_type"] == "baseline"
    assert meta["status"] == "pending"
    assert meta["baseline_output_path"] is None
    assert meta["time_path"] is None

    runs = case.gen_data["ogc-runs"]
    assert runs == [{"RunId": "run_0", "RunName": "Base",
                     "RunType": "baseline", "baseline_run_name": None}]


def test_reform_before_baseline_completed_is_refused(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})  # still pending
    resp = case.create_run("Ref", "reform", "Base", params={})
    assert resp["status_code"] == "error"
    assert "completed" in resp["message"].lower()
    # the reform run must not have been created
    assert not (case.res_path / "Ref").exists()


def test_reform_after_baseline_completed_links_baseline(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    case.update_run_status("Base", "completed", time_path=True)

    resp = case.create_run("Ref", "reform", "Base", params={})
    assert resp["status_code"] == "success"

    meta = case.get_run_meta("Ref")
    assert meta["run_type"] == "reform"
    assert meta["baseline_output_path"].endswith("Base")

    runs = case.gen_data["ogc-runs"]
    assert [r["RunId"] for r in runs] == ["run_0", "run_1"]
    assert runs[1]["baseline_run_name"] == "Base"


def test_reform_without_baseline_name_is_refused(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    resp = case.create_run("Ref", "reform", None, params={})
    assert resp["status_code"] == "error"


def test_duplicate_run_name_is_exist(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    resp = case.create_run("Base", "baseline", None, params={})
    assert resp["status_code"] == "exist"


def test_create_run_in_missing_case_is_error(isolated_storage):
    resp = OGCoreCase("Ghost").create_run("Base", "baseline", None, params={})
    assert resp["status_code"] == "error"


# ── get_runs / delete_run ───────────────────────────────────────────────────

def test_get_runs_reports_status_and_does_not_mutate_gendata(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})

    runs = case.get_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "pending"
    assert runs[0]["RunName"] == "Base"

    # the transient status fields must NOT leak back into genData.json on disk
    raw = File.readFile(case.gen_data_path)
    assert set(raw["ogc-runs"][0].keys()) == {"RunId", "RunName", "RunType", "baseline_run_name"}


def test_delete_run_removes_dir_and_index_entry(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    assert (case.res_path / "Base").exists()

    resp = case.delete_run("Base")
    assert resp["status_code"] == "success"
    assert not (case.res_path / "Base").exists()
    assert case.gen_data["ogc-runs"] == []


def test_get_runs_missing_case_returns_empty(isolated_storage):
    assert OGCoreCase("Ghost").get_runs() == []


# ── End-to-end through the HTTP routes ──────────────────────────────────────

def test_http_case_run_roundtrip(ogc_client):
    # create case
    r = ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    assert r.status_code == 200 and r.get_json()["status_code"] == "created"

    # it shows up in the case list
    assert ogc_client.get("/ogc/getCases").get_json() == ["Kenya"]

    # saving again edits (does not duplicate or error)
    r = ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    assert r.get_json()["status_code"] == "edited"

    # create a baseline run
    r = ogc_client.post("/ogc/createRun",
                        json={"casename": "Kenya", "run_name": "Base", "run_type": "baseline"})
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"

    # getRuns shows it as pending
    runs = ogc_client.post("/ogc/getRuns", json={"casename": "Kenya"}).get_json()
    assert len(runs) == 1 and runs[0]["status"] == "pending"

    # delete the run
    r = ogc_client.post("/ogc/deleteRun", json={"casename": "Kenya", "run_name": "Base"})
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"
    assert ogc_client.post("/ogc/getRuns", json={"casename": "Kenya"}).get_json() == []
