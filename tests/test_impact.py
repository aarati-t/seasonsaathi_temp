"""P5b impact simulator (unit): replay purity, cache invalidation, KPI maths on hand-checked fixtures."""
import copy

from features import impact, verifier_bench
from tests.fakes import demo_repo
from tools.stress import get_plot_stress_facts, stress_level

AGENT_PASS = [{"step": "verifier_agent", "detail": {"status": "PASS", "issues": []}}]


def money(v, lo=None, hi=None):
    return {"value": v, "low": v if lo is None else lo, "high": v if hi is None else hi, "fact_id": "F-x"}


def f4_result(recommended="SELL_ONE_KEEP_REST", **over):
    r = {"orchestrator_used": "adk", "verifier_status": "PASS", "withheld": False, "applicable": True,
         "recommended": recommended, "trace": AGENT_PASS, "options": [
             {"name": "KEEP_AND_BUY_FODDER", "cash_needed": money(45360, 38556, 56700), "asset_value_kept": money(45000, 40000, 50000),
              "sale_proceeds": None},
             {"name": "SELL_ONE_KEEP_REST", "cash_needed": money(22680, 19278, 28350), "asset_value_kept": money(22500, 20000, 25000),
              "sale_proceeds": money(17438, 16875, 18000)},
             {"name": "SELL_NOW", "cash_needed": money(0), "asset_value_kept": money(0),
              "sale_proceeds": money(34875, 33750, 36000)}]}
    return {**r, **over}


def f3_result(rec, base_range, rec_range, **over):
    cov = {"Chana only": "gram", "Chana + safflower": "gram-safflower", "Jowar + fodder": "sorghum"}
    facts = []
    for name, key in cov.items():
        rng = base_range if name == "Chana only" else rec_range if name == rec else [0, 0]
        facts.append({"fact_id": f"F-wb-coverage-{key}", "available": rng is not None, "range": rng})
    return {"orchestrator_used": "adk", "verifier_status": "PASS", "withheld": False, "recommended": rec,
            "trace": AGENT_PASS, "facts": facts, **over}


def test_replay_writes_no_outcomes_makes_no_model_calls_and_is_identical():
    repo = demo_repo()
    n_outcomes = len(repo.tables["outcomes"])
    a = impact.run(repo, households="demo", engine="adk")
    b = impact.run(repo, households="demo", engine="adk")
    assert len(repo.tables["outcomes"]) == n_outcomes and len(repo.tables["impact_runs"]) == 2
    assert a["meta"]["model_calls"] == 0 and a["meta"]["mode"] == "replay"
    assert a["run_id"] != b["run_id"] and impact.analytical(a) == impact.analytical(b)
    k1 = next(k for k in a["kpis"] if k["id"] == "K1")
    assert k1["status"] == "NOT_ESTABLISHED" and k1["value"] is None          # empty cache: null, never zero
    assert k1["exclusion_reasons"] == {"no cached assessment for this snapshot": 3}


def test_changed_fact_invalidates_the_cache_key():
    repo = demo_repo()
    before = impact.fact_snapshot("f3", "T-1", repo)
    assert impact.fact_snapshot("f3", "T-1", repo) == before                    # stable
    repo.tables["rain_series"][-1]["rain_mm"] = 99.0
    assert impact.fact_snapshot("f3", "T-1", repo) != before                    # changed observation -> new key
    k = impact.cache_key("T-1", "f3", "adk", "m", before, "v1")
    assert k != impact.cache_key("T-1", "f3", "adk", "m", before, "v2")          # changed code/config version


def test_eligibility_excludes_fallbacks_failures_withheld_and_missing_semantic_check():
    assert impact.eligibility(f4_result()) == (True, None)
    assert impact.eligibility(f4_result(orchestrator_used="rule"))[0] is False
    assert impact.eligibility(f4_result(verifier_status="FAIL"))[1] == "verifier status FAIL"
    assert impact.eligibility(f4_result(withheld=True))[1] == "recommendation withheld"
    assert "semantic verification not run" in impact.eligibility(f4_result(trace=[]))[1]


def test_k1_hand_calculated():
    pop = [{"hh_id": "A", "cattle_count": 2}, {"hh_id": "B", "cattle_count": 2}, {"hh_id": "C", "cattle_count": 0}]
    items = {("A", "f4"): {"result": f4_result()}, ("B", "f4"): {"result": f4_result(withheld=True, verifier_status="FAIL")}}
    k = impact.k1(items, pop)
    a = next(d for d in k["per_household_detail"] if d["hh_id"] == "A")
    # SELL_ONE: 17438 + 22500 - 22680 = 17258 ; SELL_NOW: 34875 + 0 - 0 = 34875 ; improvement = -17617
    assert (a["option_mid"], a["baseline_mid"], a["improvement_mid"]) == (17258, 34875, -17617)
    # low scenario: 16875 + 20000 - 19278 = 17597 vs 33750
    assert a["improvement_low"] == 17597 - 33750
    assert k["n_total"] == 2 and k["n_eligible"] == 1 and k["n_better"] == 0 and k["value"] == -17617
    assert k["exclusion_reasons"] == {"verifier status FAIL": 1}
    assert k["combined_feasibility"]["status"] == "unknown" and k["status"] == "SIMULATED"


def test_k2_paired_denominators_unknown_exclusions_and_zero_denominator():
    pop = [{"hh_id": h} for h in "ABCDE"]
    items = {("A", "f3"): {"result": f3_result("Jowar + fodder", [10, 20], [80, 120])},   # short -> not short
             ("B", "f3"): {"result": f3_result("Jowar + fodder", [10, 20], [10, 30])},    # short -> short
             ("C", "f3"): {"result": f3_result("Chana only", [90, 130], [0, 0])},          # not short -> not short
             ("D", "f3"): {"result": f3_result("Jowar + fodder", [60, 90], [80, 120])},    # straddles 70 -> excluded
             ("E", "f3"): {"result": f3_result("Jowar + fodder", None, [80, 120])}}        # unknown -> excluded
    k = impact.k2(items, pop)
    assert (k["n_eligible"], k["n_excluded"], k["n_baseline_short"], k["n_recommended_short"]) == (3, 2, 2, 1)
    assert (k["baseline_short_pct"], k["recommended_short_pct"], k["value"]) == (66.7, 33.3, 33.4)
    assert k["resolved_short_pct"] == 50.0
    none = impact.k2({("C", "f3"): {"result": f3_result("Chana only", [90, 130], [0, 0])}}, [{"hh_id": "C"}])
    assert none["resolved_short_pct"] is None and none["resolved_short_reason"]
    empty = impact.k2({}, pop)
    assert empty["value"] is None and empty["status"] == "NOT_ESTABLISHED"


def test_k3_future_observations_cannot_change_an_earlier_cutoff():
    repo = demo_repo()
    early = get_plot_stress_facts("P-T-1", repo, cutoff="2026-06-05")
    future = copy.deepcopy(repo.tables["rain_series"][-1])
    repo.tables["rain_series"].append({**future, "date": "2026-07-01", "rain_mm": 500.0, "cum_deficit_pct": -900.0})
    repo.tables["ndvi_series"].append({"plot_id": "P-T-1", "date": "2026-06-01", "ndvi": 0.1, "ndvi_normal": 0.5,
                                       "source": "Sentinel-2"})          # window 06-01..06-10 ends after the cutoff
    assert get_plot_stress_facts("P-T-1", repo, cutoff="2026-06-05") == early
    assert stress_level(get_plot_stress_facts("P-T-1", repo, cutoff="2026-06-10"))[1] != stress_level(early)[1]


def test_k4_cases_are_labelled_by_construction_and_deterministic_checks_behave():
    fx = verifier_bench.load_fixture()
    cases = verifier_bench.build_cases(fx)
    assert sum(c["expected"] == "invalid" for c in cases) == 20 and sum(c["expected"] == "valid" for c in cases) == 10
    res = [verifier_bench.run_case(fx, c, semantic=False) for c in cases]
    out = {c["case_id"]: r["outcome"] for c, r in zip(cases, res)}
    assert all(out[f"{p}-{i}"] == "caught" for p in ("num", "missing", "invented") for i in range(1, 6))
    assert all(out[f"unsupported-{i}"] == "not_run" for i in range(1, 6))   # only the semantic check can catch these
    assert all(out[f"control-{i}"] == "accepted" for i in range(1, 11))
    s = verifier_bench.summarize("X", res + [{**res[0], "case_id": "boom", "outcome": "error"}])
    assert s["errors_caught"] == 15 and s["errors"] == ["boom"] and s["errors_executed"] == 15


def test_k5_same_records_and_gate():
    recs = [{"context": "c", "confidence": "HIGH", "hit": h, "provenance": "synthetic"} for h in (1, 0, 0, 0, 0)]
    after = impact.apply_rule(recs)
    assert all(r["after"] == "MED" for r in after)           # hit rate 0.2 < 0.7 with n = 5
    m = {"HIGH": 0.85, "MED": 0.60, "LOW": 0.35}
    assert impact.confidence_gap(after, m, "confidence") == round(abs(0.85 - 0.2), 3)
    assert impact.confidence_gap(after, m, "after") == round(abs(0.60 - 0.2), 3)
    repo = demo_repo()
    repo.tables["outcomes"] = []
    k = impact.k5(repo)
    assert k["status"] == "NOT_ESTABLISHED" and k["value"] is None and k["observed_gap"] is None


def test_k7_is_target_only():
    k = impact.k7()
    assert k["status"] == "TARGET" and k["file_generation_ms"] is None and "Not measured" in k["limitations"]


def test_k4_unmodified_fixture_passes_the_code_verifier():
    """Guards the fixture itself: if the frozen output failed on its own, controls would be false rejections."""
    from core.schemas import AssessorOutput
    from core.verifier import verify
    fx = verifier_bench.load_fixture()
    a = AssessorOutput.model_validate(fx["assessment"])
    assert verify(a, {f["fact_id"]: f for f in fx["facts"]}, [o.name for o in a.options],
                  check_why=True, check_explanations=True) == []


def test_api_replay_does_not_save_an_incomplete_run():
    import pytest
    repo = demo_repo()
    with pytest.raises(impact.CacheIncomplete):
        impact.run(repo, require_complete=True)
    assert repo.tables.get("impact_runs", []) == []        # last good run would stay "latest"
