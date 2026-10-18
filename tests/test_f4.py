"""F4 (unit, no LLM): shortfall + fodder cost maths, option sets, SELL_NOW rule, no-cattle path, F4 rules."""
import asyncio

import pytest

from core.schemas import Driver, F4AssessorOutput, F4Option, WhyNot
from features import f4_livestock as f4
from orchestrators import selector
from tests.fakes import ScriptedEngine, demo_repo
from tools import livestock

F4_TOOLS = [("get_livestock_facts", ("T-1",)), ("get_rain_status", (1,)), ("find_similar_households", ("T-1",))]


def test_shortfall_weeks():
    assert livestock.shortfall_weeks(12, 3) == 9
    assert livestock.shortfall_weeks(12, 15) == 0
    assert livestock.shortfall_weeks(12, None) is None


def test_fodder_cost_maths():
    facts = {f["fact_id"]: f for f in livestock.get_livestock_facts("T-1", demo_repo())}   # 2 cows, 3 weeks fodder
    cost = facts["F-f4-fodder-cost"]
    assert cost["kg"] == 2 * 18 * 7 * 9 == 2268
    assert (cost["range"], cost["value"]) == ([38556, 56700], 45360)            # 2268 kg x ₹17 / ₹20 / ₹25
    assert facts["F-f4-opt-SELL_ONE_KEEP_REST-cash"]["value"] == 22680          # one animal kept
    assert facts["F-f4-sale-now"]["range"] == [16875, 18000]                    # 22500 x (1 - 25%) .. (1 - 20%)
    assert facts["F-f4-loss-per-animal"]["value"] == 22500 - 17438
    assert facts["F-f4-opt-SELL_NOW-cash-in"]["range"] == [33750, 36000]
    assert "not intake-equivalent" in facts["F-f4-silage-own"]["text"]
    assert "2019 rate; current rate not confirmed" in facts["F-f4-camp"]["text"]
    assert facts["F-f4-sale-normal"]["is_synthetic"] and facts["F-f4-sale-now"]["is_synthetic"]


def test_goats_not_costed_and_unknown_fodder():
    repo = demo_repo()
    repo.tables["households"][0].update(goats_count=4, fodder_weeks_left=None)
    facts = {f["fact_id"]: f for f in livestock.get_livestock_facts("T-1", repo)}
    assert "goat fodder not costed – no sourced intake norm" in facts["F-f4-goats-note"]["text"]
    assert facts["F-f4-shortfall-weeks"]["value"] is None and "F-f4-fodder-cost" not in facts
    assert facts["F-f4-opt-KEEP_AND_BUY_FODDER-cash"]["value"] is None


@pytest.mark.parametrize("cattle,expected", [(0, []), (None, []), (1, livestock.OPTIONS_ONE), (2, livestock.OPTIONS_MANY),
                                             (6, livestock.OPTIONS_MANY)])
def test_option_sets_by_cattle_count(cattle, expected):
    assert livestock.option_set(cattle) == expected


def test_weeks_needed_for_cash_limit():
    # 2 animals x 18 kg x 7 x ₹20 = ₹5,040 per week; limit ₹10,000 -> 12 - 10000/5040 = 10.02 -> 11 weeks
    assert livestock.weeks_needed_for_limit(2, 18, 12, 20, 10000) == 11
    assert livestock.weeks_needed_for_limit(1, 18, 12, 20, 60000) == 0


def output(risks=(60, 40, 80), names=livestock.OPTIONS_MANY, text_extra=""):
    opts = [F4Option(name=n, cash_pressure="med", risk=r, confidence="HIGH", support_ids=[],
                     drivers=[Driver(text=f"Cash needed for {n}{text_extra}", fact_id=f"F-f4-opt-{n}-cash")])
            for n, r in zip(names, risks)]
    rec = names[1]
    return F4AssessorOutput(options=opts, recommended=rec,
                            why_recommended=[Driver(text="Less fodder to buy", fact_id=f"F-f4-opt-{rec}-cash")],
                            why_not_others=[WhyNot(option=n, text="cash need", fact_id=f"F-f4-opt-{n}-cash")
                                            for n in names if n != rec],
                            explanation_mr="mr", explanation_hi="hi")


def test_sell_now_never_recommended_on_a_tie_or_worse():
    order = livestock.OPTIONS_MANY
    assert f4.choose_recommended(output((50, 40, 40)), order) == "SELL_ONE_KEEP_REST"   # tie -> not SELL_NOW
    assert f4.choose_recommended(output((50, 40, 30)), order) == "SELL_NOW"             # strictly lowest
    assert f4.choose_recommended(output((20, 40, 90)), order) == "KEEP_AND_BUY_FODDER"


def run(hh="T-1", repo=None, **engines):
    return asyncio.run(selector.run("f4", hh, engine="adk", repo=repo or demo_repo(), engines=engines))


def test_no_cattle_is_not_applicable_without_llm():
    repo = demo_repo()
    repo.tables["households"][0]["cattle_count"] = 0
    eng = ScriptedEngine(outputs=[output()], tool_calls=F4_TOOLS)
    r = run(repo=repo, adk=eng)
    assert r.applicable is False and r.reason == "no large animals" and eng.runs == []


def test_pipeline_fills_money_from_facts_and_explains():
    r = run(adk=ScriptedEngine(outputs=[output()], tool_calls=F4_TOOLS))
    assert r.verifier_status == "PASS" and r.recommended == "SELL_ONE_KEEP_REST"
    keep = next(o for o in r.options if o.name == "KEEP_AND_BUY_FODDER")
    assert (keep.cash_needed.low, keep.cash_needed.high) == (38556, 56700)
    assert keep.cash_needed.fact_id == "F-f4-opt-KEEP_AND_BUY_FODDER-cash"
    assert r.explain.what_would_change["threshold"] == 11
    assert r.explain.why_recommended and len(r.explain.why_not_others) == 2
    assert any(t.step == "evidence" for t in r.trace)


def test_moneylender_and_unknown_support_are_rejected():
    bad = output(text_extra=", borrow from the sahukar")
    bad.options[0].support_ids = ["crop_loan_waiver"]
    issues = f4.f4_rule_issues(bad, None)
    assert any("moneylender" in i for i in issues) and any("support_ids" in i for i in issues)


def test_fodder_camp_needs_2019_caveat():
    a = output()
    a.options[0].drivers.append(Driver(text="Camp support is ₹100 per animal per day", fact_id="F-f4-camp"))
    assert any("2019" in i for i in f4.f4_rule_issues(a, None))
    a.options[0].drivers[-1].text += " (2019 rate; current rate not confirmed)"
    assert not f4.f4_rule_issues(a, None)
    a.explanation_mr = "चारा छावणीत प्रति जनावर दररोज १०० रुपये मदत मिळते."      # rate quoted, no 2019
    assert any("2019" in i for i in f4.f4_rule_issues(a, None))
    a.explanation_mr = "चारा छावणीत दररोज १०० रुपये मदत (२०१९ चा दर; सध्याचा दर निश्चित नाही)."
    assert not f4.f4_rule_issues(a, None)


def test_unknown_fodder_weeks_caps_low():
    repo = demo_repo()
    repo.tables["households"][0]["fodder_weeks_left"] = None
    out = output()
    for o in out.options:      # cash facts have no number now; drivers carry none either
        o.drivers = [Driver(text="Cash need not known", fact_id=f"F-f4-opt-{o.name}-cash")]
    r = run(repo=repo, adk=ScriptedEngine(outputs=[out, out], tool_calls=F4_TOOLS))
    assert all(o.confidence == "LOW" for o in r.options)
    assert any("fodder weeks left unknown" in n for n in r.notes)
