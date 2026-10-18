"""F3 pipeline: verification loop, injected-error demo, caps, safety floor, engine fallback chain.
Unit tests use a scripted engine and an in-memory repo; the repeatability test is marked llm."""
import asyncio
import os

import pytest

from orchestrators import selector
from tests.fakes import ScriptedEngine, demo_repo, good_output

from tests.fakes import cal_facts  # noqa: E402

CAL = cal_facts()


def run(hh="T-2", inject=None, repo=None, **engines):
    repo = repo or demo_repo()
    return asyncio.run(selector.run("f3", hh, inject=inject, engine="adk", repo=repo, engines=engines)), repo


def test_pass_path_logs_and_applies_calibration_rule():
    r, repo = run(adk=ScriptedEngine(outputs=[good_output(CAL)]))
    assert r.verifier_status == "PASS" and r.orchestrator_used == "adk" and r.fallback_reason is None
    conf = {o.name: o.confidence for o in r.options}
    assert conf["Chana only"] == "MED"            # HIGH hit 0.33 < 0.70 with n=6 -> downgrade
    assert conf["Chana + safflower"] == "HIGH"    # n=4 < 5 -> no downgrade, insufficient history
    assert conf["Jowar + fodder"] == "HIGH"       # hit 0.8 >= 0.70 -> kept
    cal = next(t for t in r.trace if t.step == "calibration" and "downgrade" in t.title)
    assert "Chana only" in cal.title
    assert any("insufficient history (n=4)" in n for n in r.notes)
    assert {t.step for t in r.trace} >= {"engine", "facts", "verifier_code", "calibration", "safety", "usage"}
    log = repo.tables["decision_log"]
    assert len(log) == 1 and log[0]["orchestrator_used"] == "adk" and log[0]["verifier_status"] == "PASS"


def test_injected_bad_fact_id_is_caught_then_regenerated():
    eng = ScriptedEngine(outputs=[good_output(CAL), good_output(CAL)])
    r, _ = run(inject="bad_fact_id", adk=eng)
    code_steps = [t for t in r.trace if t.step == "verifier_code"]
    assert "FAIL" in code_steps[0].title and "F-corrupted-999" in str(code_steps[0].detail)
    assert "PASS" in code_steps[-1].title and r.verifier_status == "PASS"
    assert len(eng.runs) == 2 and eng.runs[1].planner_prompt is None  # regeneration skips the planner


def test_injected_bad_number_is_caught():
    eng = ScriptedEngine(outputs=[good_output(CAL), good_output(CAL)])
    r, _ = run(inject="bad_number", adk=eng)
    first = next(t for t in r.trace if t.step == "verifier_code")
    assert "does not match fact" in str(first.detail)


def test_second_failure_downgrades_to_low():
    bad = good_output(CAL)
    bad.options[0].drivers[0].fact_id = "F-nope"
    r, _ = run(adk=ScriptedEngine(outputs=[bad, bad]))
    assert r.verifier_status == "FAIL" and all(o.confidence == "LOW" for o in r.options)


def test_safety_floor_in_pipeline():
    # T-1 has cash buffer low; the assessor says water short + ACT
    r, _ = run(hh="T-1", adk=ScriptedEngine(outputs=[good_output(CAL, signal="ACT", water="short")]))
    assert r.safety_rule_applied and all(o.signal != "ACT" for o in r.options)


def test_planner_skipping_required_tools_is_repaired():
    eng = ScriptedEngine(outputs=[good_output(CAL), good_output(CAL)], call_tools=False)
    r, _ = run(adk=eng)
    assert any("Required tools called by code" in t.title for t in r.trace)
    assert "F-rain-deficit" in {f.fact_id for f in r.facts} and len(eng.runs) == 2


def test_adk_error_falls_back_to_plain():
    r, repo = run(adk=ScriptedEngine(error=RuntimeError("boom")), plain=ScriptedEngine(name="plain", outputs=[good_output(CAL)]))
    assert r.orchestrator_used == "plain" and "adk: RuntimeError: boom" in r.fallback_reason
    assert repo.tables["decision_log"][0]["fallback_reason"] == r.fallback_reason


def test_all_engines_fail_uses_cache_then_rule(monkeypatch):
    repo = demo_repo()
    ok, _ = run(repo=repo, adk=ScriptedEngine(outputs=[good_output(CAL)]))
    broken = dict(adk=ScriptedEngine(error=RuntimeError("down")), plain=ScriptedEngine(name="plain", error=RuntimeError("down")))
    cached, _ = run(repo=repo, **broken)
    assert cached.orchestrator_used == "cache" and cached.recommended == ok.recommended

    monkeypatch.setenv("RESULT_CACHE", "off")
    rule, _ = run(repo=repo, **broken)
    assert rule.orchestrator_used == "rule" and rule.verifier_status == "RULE"
    assert all(o.confidence == "LOW" and o.failure_risk is None for o in rule.options)


@pytest.mark.llm
def test_repeatability_3_of_3(monkeypatch):
    """SPEC F3 acceptance: same input -> same signals in 3 runs (temperature 0). Cache off so each run is real."""
    monkeypatch.setenv("LLM_CACHE", "off")
    monkeypatch.setenv("ENGINE_TIMEOUT_S", os.getenv("ENGINE_TIMEOUT_S", "60"))
    results = [asyncio.run(selector.run("f3", "SYN-008")) for _ in range(3)]
    assert all(r.verifier_status == "PASS" for r in results), [r.notes for r in results]
    shown = [(r.recommended, next(o.signal for o in r.options if o.name == r.recommended)) for r in results]
    assert shown.count(shown[0]) == 3, shown        # what the farmer sees: same recommendation + signal 3/3
    per_option = [tuple(sorted((o.name, o.signal) for o in r.options)) for r in results]
    print(f"all option signals identical in {per_option.count(per_option[0])}/3 runs: {per_option}")


def test_recommended_is_picked_from_scores_not_assessor_text():
    out = good_output(CAL, signal="WAIT")
    out.options[0].failure_risk, out.options[1].failure_risk, out.options[2].failure_risk = 33, 25, 80
    out.recommended = "Chana only"
    r, _ = run(adk=ScriptedEngine(outputs=[out]))
    assert r.recommended == "Chana + safflower" and any("Recommended set to" in n for n in r.notes)


def test_recommended_prefers_higher_confidence_among_equal_signals():
    out = good_output(CAL, signal="WAIT")
    out.options[0].failure_risk, out.options[1].failure_risk, out.options[2].failure_risk = 35, 25, 80
    for o, c in zip(out.options, ("MED", "LOW", "MED")):   # MED is never touched by the calibration rule
        o.confidence = c                                  # option 2: lower risk but less sure
    r, _ = run(adk=ScriptedEngine(outputs=[out]))
    assert r.recommended == "Chana only"


def test_thin_profile_caps_confidence_at_low():
    repo = demo_repo()
    thin = dict(repo.tables["households"][1], hh_id="T-THIN", cash_buffer_band=None, cattle_count=None,
                goats_count=None, fodder_weeks_left=None, off_farm_income=None, water_hours_per_week=None,
                primary_cultivator_gender=None, crops_last_season=None)
    repo.tables["households"].append(thin)
    r, _ = run(hh="T-THIN", repo=repo, adk=ScriptedEngine(outputs=[good_output(CAL)]))
    assert all(o.confidence == "LOW" for o in r.options)
    assert any(t.title == "Confidence lowered: profile incomplete" for t in r.trace)
    assert any("profile incomplete" in n for n in r.notes)


def test_household_without_plot_reports_no_ndvi_and_lowers_one_level():
    repo = demo_repo()
    repo.tables["plots"] = [p for p in repo.tables["plots"] if p["hh_id"] != "T-3"]
    for h in repo.tables["households"]:   # soil type from the farmer keeps the water budget available (P7)
        if h["hh_id"] == "T-3":
            h["soil_type_farmer"] = "black cotton soil (vertisol)"
    out = good_output(cal_facts("well"))                # T-3 is irrigated: no calibration history -> n=0
    r, _ = run(hh="T-3", repo=repo, adk=ScriptedEngine(outputs=[out]))
    assert "F-ndvi-unavailable" in {f.fact_id for f in r.facts}
    assert any("crop health (NDVI) unavailable for this household" in n for n in r.notes)
    assert all(o.confidence == "MED" for o in r.options)   # HIGH lowered one level


def test_trace_and_log_carry_backend_and_model():
    r, repo = run(adk=ScriptedEngine(outputs=[good_output(CAL)]))
    assert r.genai_backend == "test" and r.gemini_model
    assessor = next(t for t in r.trace if t.step == "assessor") if any(t.step == "assessor" for t in r.trace) else None
    assert all(t.backend for t in r.trace)
    assert {t.backend for t in r.trace if t.step in ("verifier_code", "safety", "facts")} == {"code"}
    log = repo.tables["decision_log"][0]
    assert log["genai_backend"] == "test" and log["gemini_model"] == r.gemini_model
    assert assessor is None or assessor.backend == "test"


def test_run_f3_forced_failure_engine_can_be_created():
    """--force-adk-fail must keep working when the Engine interface grows (P3 added run_agents)."""
    import run_f3
    with pytest.raises(RuntimeError, match="forced ADK failure"):
        asyncio.run(run_f3.ForcedFailure().run_agents("f3", [], {}))


def test_persistent_verifier_failure_withholds_the_recommendation():
    bad = good_output(CAL)
    bad.options[0].drivers[0].fact_id = "F-made-up-1"
    r, _ = run(adk=ScriptedEngine(outputs=[bad, bad]))
    assert r.verifier_status == "FAIL" and r.withheld and "verification failed" in r.withheld_reason
