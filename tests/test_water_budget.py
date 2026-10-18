"""P7 soil + water-budget grounding (unit): arithmetic in code, honest 'not available', verifier consistency."""
import asyncio

from features import f1_interview
from features.f3_cropmix import lower_where_no_water_budget, water_fit_issues
from orchestrators.base import FactStore
from orchestrators import selector
from tests.fakes import ScriptedEngine, cal_facts, demo_repo, good_output
from tools import soil, water_budget as wb

CLAY = {"theta_fc": [0.32, 0.40], "theta_wp": [0.20, 0.24]}   # FAO-56 Table 19


def real_need(repo):
    """The sourced rows from data/crop_water_need.csv (values copied, not the wide test rows)."""
    repo.tables["crop_water_need"] = [
        {"crop_key": "gram", "crop": "Chickpea", "season": "rabi", "need_mm_low": 356.5, "need_mm_high": 356.5,
         "root_depth_m_low": 0.6, "root_depth_m_high": 1.0, "depletion_p": 0.5, "region": "Rajasthan", "basis": "reported",
         "is_synthetic": False, "note": "Kumar et al. 2019", "source_url": "https://agris.fao.org/x", "critical_stage": None,
         "root_source_url": None},
        {"crop_key": "safflower", "crop": "Safflower", "season": "rabi", "need_mm_low": 377, "need_mm_high": 377,
         "root_depth_m_low": 1.0, "root_depth_m_high": 2.0, "depletion_p": 0.6, "region": "Rajasthan", "basis": "reported",
         "is_synthetic": False, "note": "Singh et al.", "source_url": "https://epubs.icar.org.in/x", "critical_stage": None,
         "root_source_url": None},
        {"crop_key": "sorghum", "crop": "Rabi sorghum", "season": "rabi", "need_mm_low": 425, "need_mm_high": 610,
         "root_depth_m_low": 1.0, "root_depth_m_high": 2.0, "depletion_p": 0.55, "region": "India", "basis": "reported",
         "is_synthetic": False, "note": "ICAR-IIMR", "source_url": "https://millets.res.in/x", "critical_stage": None,
         "root_source_url": None}]
    return repo


def facts_by_id(facts):
    return {f["fact_id"]: f for f in facts}


def test_stored_water_uses_wilting_point_and_caps_at_field_capacity():
    assert wb.stored_mm(0.30, CLAY, 1.0) == (60, 100)       # (0.30-0.24)*1000 .. (0.30-0.20)*1000
    assert wb.stored_mm(0.50, CLAY, 0.6) == (48, 120)       # capped: (0.32-0.24)*600 .. (0.40-0.20)*600
    assert wb.stored_mm(0.15, CLAY, 1.0) == (0, 0)          # below wilting point: nothing plant-available


def test_irrigation_mm_rainfed_hours_and_unknown():
    assert wb.irrigation_mm({"irrigation": "rainfed"})[0] == 0
    mm, how, assumed = wb.irrigation_mm({"irrigation": "well", "water_hours_per_week": 10, "acres": 3})
    assert mm == round(10 * 16 * 10 / (3 * 4046.86) * 1000) and assumed and "assumption" in how
    assert wb.irrigation_mm({"irrigation": "well", "water_hours_per_week": None, "acres": 3})[0] is None
    assert wb.irrigation_mm({"irrigation": "well", "water_hours_per_week": 5, "acres": None})[0] is None


def test_bands_from_coverage_range():
    assert wb.band(40) == "short" and wb.band(85) == "tight" and wb.band(120) == "fit"
    assert wb.allowed_bands(60, 90) == ["short", "tight"] and wb.allowed_bands(110, 140) == ["fit"]


def test_rainfed_budget_with_sourced_needs():
    repo = real_need(demo_repo())
    repo.tables["soil_props"][0]["texture_class"] = "clay"
    repo.tables["soil_moisture"][0]["sm_rootzone"] = 0.30
    f = facts_by_id(wb.get_water_budget("T-1", repo))                    # T-1: rainfed, plot soil = clay
    gram = f["F-wb-coverage-gram"]
    assert gram["available"] and gram["inputs"]["stored_mm"] == [36, 60]  # 0.6 m root depth (FAO-56 Table 22)
    assert gram["range"] == [round(36 / 356.5 * 100), round(60 / 356.5 * 100)] == [10, 17]
    mix = f["F-wb-coverage-gram-safflower"]                               # mix: larger need (377), shallower roots
    assert mix["inputs"]["need_mm"] == [377, 377] and mix["inputs"]["root_depth_m"] == 0.6
    assert f["F-wb-irrigation"]["value"] == 0 and f["F-wb-irrigation"]["basis"] == "derived"   # no pump assumption
    assert f["F-wb-need-sorghum"]["range"] == [425, 610] and f["F-wb-need-gram"]["caveat"] == "measured in Rajasthan"


def test_missing_inputs_give_not_available_never_a_number():
    repo = demo_repo()
    repo.tables["plots"] = [p for p in repo.tables["plots"] if p["hh_id"] != "T-2"]
    f = facts_by_id(wb.get_water_budget("T-2", repo))                    # no plot, no farmer soil
    assert not f["F-wb-coverage-gram"]["available"] and f["F-wb-coverage-gram"]["value"] is None
    assert "soil texture unknown" in f["F-wb-coverage-gram"]["text"]
    repo.tables["crop_water_need"] = [r for r in repo.tables["crop_water_need"] if r["crop_key"] != "safflower"]
    for h in repo.tables["households"]:
        if h["hh_id"] == "T-2":
            h["soil_type_farmer"] = "black cotton soil (vertisol)"
    f = facts_by_id(wb.get_water_budget("T-2", repo))
    assert f["F-wb-coverage-gram"]["available"] and f["F-wb-coverage-gram"]["inputs"]["texture_fact"] == "F-soil-farmer"
    assert "no sourced water need for safflower" in f["F-wb-coverage-gram-safflower"]["text"]
    repo.tables["soil_props"][0]["texture_class"] = "clay loam"            # not in FAO-56 Table 19
    assert "not in FAO-56 Table 19" in facts_by_id(wb.get_water_budget("T-1", repo))["F-wb-coverage-gram"]["text"]


def test_soil_status_facts_and_unavailable():
    f = facts_by_id(soil.get_soil_status("T-1", demo_repo()))
    assert f["F-soil-texture"]["value"] == "loam" and "location SYNTHETIC" in f["F-soil-texture"]["text"]
    assert f["F-soil-rootzone"]["value"] == 0.25 and f["F-soil-rootzone"]["as_of"] == "2026-10-05"
    repo = demo_repo()
    repo.tables["soil_moisture"], repo.tables["soil_props"] = [], []
    f = facts_by_id(soil.get_soil_status("T-1", repo))
    assert {"F-soil-map-unavailable", "F-soil-moisture-unavailable"} <= set(f)


def test_verifier_flags_water_fit_against_coverage_and_lowers_when_unavailable():
    store = FactStore()
    store.add([{"fact_id": "F-wb-coverage-gram", "available": True, "range": [10, 17], "text": "x"},
               {"fact_id": "F-wb-coverage-gram-safflower", "available": True, "range": [80, 120], "text": "x"},
               {"fact_id": "F-wb-coverage-sorghum", "available": False, "value": None, "text": "not available"}])
    out = good_output(cal_facts(), water="fit")
    issues = water_fit_issues(out, store)
    assert any("Chana only: water_fit 'fit' contradicts F-wb-coverage-gram" in i for i in issues)
    assert not any(i.startswith("Chana + safflower") for i in issues)    # 80-120 spans tight..fit
    notes = lower_where_no_water_budget(out, store)
    assert notes == ["Confidence lowered: Jowar + fodder HIGH -> MED (water budget not available)"]


def test_f3_cites_coverage_and_regenerates_on_contradiction():
    repo = real_need(demo_repo())
    repo.tables["soil_props"][0]["texture_class"] = "clay"
    wrong, right = good_output(cal_facts(), water="fit"), good_output(cal_facts(), water="short")
    r = asyncio.run(selector.run("f3", "T-1", engine="adk", repo=repo, engines={"adk": ScriptedEngine(outputs=[wrong, right])}))
    fails = [t for t in r.trace if t.step == "verifier_code" and t.detail["issues"]]
    assert fails and any("contradicts F-wb-coverage" in i for i in fails[0].detail["issues"])
    assert r.verifier_status == "PASS" and all(o.water_fit == "short" for o in r.options)
    assert {"F-soil-texture", "F-soil-rootzone", "F-wb-coverage-gram"} <= {f.fact_id for f in r.facts}


def test_f1_records_soil_only_when_stated_and_keeps_old_profile_text():
    turns = [f1_interview.Turn(role="user", text="आमची काळी माती आहे, 3 एकर")]
    ex = f1_interview.rule_extract(turns)
    profile, _ = f1_interview.build_profile(ex, turns)
    assert profile["soil_type_farmer"] == "black cotton soil (vertisol)"
    assert "soil_type_farmer" not in f1_interview.unknown_fields({**profile, "soil_type_farmer": None})
    old = {"tenure": "owner", "acres": 2}
    assert "soil type" not in f1_interview.build_profile_text(old)
    assert "soil type: black cotton soil (vertisol)" in f1_interview.build_profile_text({**old, **profile})
