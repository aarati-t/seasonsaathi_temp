"""Explainability (unit): what_would_change thresholds computed in code, confidence_reason, data_gaps, checks."""
import asyncio

from core.schemas import Driver
from core.verifier import verify
from orchestrators import selector
from tests.fakes import ScriptedEngine, cal_facts, demo_repo, good_output

CAL = cal_facts()


def run(hh, out, repo=None):
    return asyncio.run(selector.run("f3", hh, engine="adk", repo=repo or demo_repo(),
                                    engines={"adk": ScriptedEngine(outputs=[out, out])}))


def test_safety_rule_gives_rain_deficit_threshold():
    r = run("T-1", good_output(CAL, signal="ACT", water="short"))      # cash low + water short -> safety rule
    w = r.explain.what_would_change
    assert (w["fact_id"], w["current"], w["threshold"]) == ("F-rain-deficit", 87.5, 25)
    assert "below 25% (now 87.5%)" in w["text"]
    assert any(c.lower().startswith("safety rule") for c in r.explain.confidence_reason)


def test_calibration_downgrade_gives_hit_rate_threshold():
    r = run("T-2", good_output(CAL, signal="WAIT"))                    # cash med: no safety rule
    w = r.explain.what_would_change
    assert (w["fact_id"], w["current"], w["threshold"]) == ("F-cal-gt30-rainfed-gram", 33, 70)
    assert any(c.lower().startswith("calibration") for c in r.explain.confidence_reason)
    assert r.explain.evidence["text"].startswith("evidence used: ")


def test_data_gaps_list_synthetic_inputs_used():
    r = run("T-2", good_output(CAL, signal="WAIT"))
    assert any(g.startswith("synthetic: F-cal-gt30-rainfed-gram") for g in r.explain.data_gaps)


def test_why_fields_and_explanation_numbers_are_verified():
    facts = {"F-hh-irrigation": {"fact_id": "F-hh-irrigation", "text": "Irrigation source: rainfed", "value": "rainfed"},
             "F-rain-deficit": {"fact_id": "F-rain-deficit", "text": "Season rain is 41.9% below normal", "value": 41.9},
             **{f"F-wb-coverage-{k}": {"fact_id": f"F-wb-coverage-{k}", "text": "coverage", "value": None}
                for k in ("gram", "gram-safflower", "sorghum")}}
    names = ["Chana only", "Chana + safflower", "Jowar + fodder"]
    out = good_output("F-hh-irrigation")
    assert verify(out, facts, names, check_why=True, check_explanations=True) == []
    out.why_recommended = []
    out.why_not_others = out.why_not_others[:1]
    out.explanation_mr = "पाऊस ४२% कमी आहे आणि ७ दिवस कोरडे"            # 42 ~ 41.9 is fine, 7 is not a fact
    issues = verify(out, facts, names, check_why=True, check_explanations=True)
    assert any("why_recommended needs 1-3" in i for i in issues)
    assert any("missing a line for Jowar + fodder" in i for i in issues)
    assert any("number 7 is not in any fact" in i for i in issues) and not any("number 42" in i for i in issues)
