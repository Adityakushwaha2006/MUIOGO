"""
OG-Core case backup / restore.

Backup zips the whole case folder; restore validates and unpacks it. Tests cover
the zip contents, the validity checks (must be an OG-Core backup, not a CLEWS
one, not corrupt, not a duplicate, no path traversal), and a full
backup -> delete -> restore round-trip. Fast, no model solves.
"""

import io
import json
import zipfile

from Classes.OGCore.OGCoreClass import OGCoreCase


def _make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _restore(ogc_client, zip_bytes):
    return ogc_client.post(
        "/ogc/restoreCase",
        data={"file": (io.BytesIO(zip_bytes), "backup.zip")},
        content_type="multipart/form-data",
    )


# ── Backup ──────────────────────────────────────────────────────────────────

def test_backup_returns_zip_with_expected_entries(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    ogc_client.post("/ogc/createRun",
                    json={"casename": "Kenya", "run_name": "Base", "run_type": "baseline"})

    r = ogc_client.get("/ogc/backupCase", query_string={"casename": "Kenya"})
    assert r.status_code == 200

    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert "Kenya/genData.json" in names
    assert any(n.endswith("run_meta.json") for n in names)


def test_backup_missing_casename_is_400(ogc_client):
    assert ogc_client.get("/ogc/backupCase").status_code == 400


# ── Restore validation ──────────────────────────────────────────────────────

def test_restore_valid_ogcore_backup(ogc_client):
    zip_bytes = _make_zip({"Restored/genData.json":
                           json.dumps({"ogc-casename": "Restored", "ogc-runs": []})})
    r = _restore(ogc_client, zip_bytes)
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"
    assert "Restored" in ogc_client.get("/ogc/getCases").get_json()


def test_restore_rejects_clews_backup(ogc_client):
    # CLEWS backups carry osy-casename, not ogc-casename
    zip_bytes = _make_zip({"Clews/genData.json":
                           json.dumps({"osy-casename": "Clews", "osy-cases": []})})
    r = _restore(ogc_client, zip_bytes)
    assert r.status_code == 400
    assert "ogc-casename" in r.get_json()["message"]


def test_restore_rejects_non_zip(ogc_client):
    r = _restore(ogc_client, b"this is not a zip file")
    assert r.status_code == 400


def test_restore_rejects_zip_without_gendata(ogc_client):
    zip_bytes = _make_zip({"Foo/notes.txt": "hello"})
    r = _restore(ogc_client, zip_bytes)
    assert r.status_code == 400
    assert "genData.json" in r.get_json()["message"]


def test_restore_rejects_existing_case(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Dup"}})
    zip_bytes = _make_zip({"Dup/genData.json":
                           json.dumps({"ogc-casename": "Dup", "ogc-runs": []})})
    r = _restore(ogc_client, zip_bytes)
    assert r.get_json()["status_code"] == "exist"


def test_restore_rejects_path_traversal_entries(ogc_client):
    zip_bytes = _make_zip({
        "Trav/genData.json": json.dumps({"ogc-casename": "Trav", "ogc-runs": []}),
        "../evil.txt": "pwned",
    })
    r = _restore(ogc_client, zip_bytes)
    assert r.status_code == 400
    assert "unsafe" in r.get_json()["message"].lower()


def test_restore_missing_file_is_400(ogc_client):
    r = ogc_client.post("/ogc/restoreCase", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


# ── Full round-trip ─────────────────────────────────────────────────────────

def test_backup_delete_restore_roundtrip(ogc_client, isolated_storage):
    # build a case with a run and params
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya", "ogc-desc": "demo"})
    case.create_run("Base", "baseline", None, params={"frisch": 0.41})

    # backup -> bytes
    backup = ogc_client.get("/ogc/backupCase", query_string={"casename": "Kenya"}).data

    # delete the case directory
    case.delete_case()
    assert not case.case_path.exists()

    # restore from the backup bytes
    r = _restore(ogc_client, backup)
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"

    # case, run and params all came back intact
    restored = OGCoreCase("Kenya")
    assert restored.gen_data["ogc-desc"] == "demo"
    assert [run["RunName"] for run in restored.get_runs()] == ["Base"]
    assert restored.get_params("Base") == {"frisch": 0.41}
