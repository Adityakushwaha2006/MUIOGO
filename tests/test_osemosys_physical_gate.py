from scripts.validate_osemosys_physical_gate import evaluate_annual


def annual_result(demand: float, ceiling: float):
    return evaluate_annual(
        year="2030",
        direct_demand={"service": demand},
        technologies=("producer",),
        routes_by_output={"service": [("producer", 1, 1.0)]},
        inputs_by_route={},
        activity_ceiling={"producer": ceiling},
        tech_name={"producer": "GENERIC_PRODUCER"},
        commodity_name={"service": "GENERIC_SERVICE"},
    )


def test_annual_aad_envelope_passes_without_timeslice_profile():
    result = annual_result(demand=9.0, ceiling=10.0)
    assert result["status"] == "passed"
    assert result["period"] == "annual"
    assert result["failures"] == []


def test_annual_aad_envelope_reports_generic_shortfall():
    result = annual_result(demand=11.0, ceiling=10.0)
    assert result["status"] == "failed"
    assert result["failures"] == [{
        "kind": "commodity_annual_shortfall",
        "year": "2030",
        "commodity": "GENERIC_SERVICE",
        "commodity_id": "service",
        "producers": [{
            "technology": "GENERIC_PRODUCER",
            "technology_id": "producer",
            "mode": 1,
            "output_capacity_annual_activity": 10.0,
        }],
        "required_annual_activity": 11.0,
        "optimistic_production_upper_annual_activity": 10.0,
        "headroom_annual_activity": -1.0,
    }]
