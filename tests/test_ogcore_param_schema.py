"""
OG-Core parameter-schema endpoint (GET /ogc/getParameterSchema).

Serves the metadata the frontend needs to build parameter input forms, read from
the installed OG-Core so it never drifts from the running version. Fast: the
schema is located via find_spec + json.load, so these tests neither import the
heavy ogcore package nor solve anything.
"""

import json

from Classes.OGCore.OGCoreRunnerClass import load_parameter_schema


# ── Schema content ──────────────────────────────────────────────────────────

def test_schema_is_nonempty_dict():
    schema = load_parameter_schema()
    assert isinstance(schema, dict) and len(schema) > 0


def test_frisch_metadata_is_complete():
    s = load_parameter_schema()["frisch"]
    assert s["title"]
    assert s["type"] == "float"
    assert s["default"] == 0.4
    assert s["min"] == 0.2 and s["max"] == 0.62
    assert s["section"]


def test_policy_param_present_with_section():
    s = load_parameter_schema()
    assert "cit_rate" in s
    assert s["cit_rate"]["section"]  # used to group params in the UI


def test_large_calibration_arrays_omit_default():
    s = load_parameter_schema()
    large = [k for k, v in s.items() if v.get("large")]
    assert large, "expected some calibration arrays to be flagged large"
    for k in large:
        assert s[k]["default"] is None


def test_schema_is_json_serializable():
    # no numpy / non-JSON types leak through
    json.dumps(load_parameter_schema())


def test_schema_is_deterministic():
    assert load_parameter_schema() == load_parameter_schema()


# ── HTTP ────────────────────────────────────────────────────────────────────

def test_http_get_parameter_schema(ogc_client):
    r = ogc_client.get("/ogc/getParameterSchema")
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body, dict)
    assert "frisch" in body and body["frisch"]["max"] == 0.62


def test_http_schema_needs_no_session(client):
    """Global metadata: works with no case / no session set."""
    assert client.get("/ogc/getParameterSchema").status_code == 200
