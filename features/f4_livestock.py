"""F4 Livestock and Fodder Decision, SPEC section 6 + P4 rules.

Flow per engine: planner (tools) -> assessor (F4AssessorOutput) -> verifier agent, then in code:
required-tool check, code verifier (+ F4 rules), one regeneration, then LOW; missing-data caps; the final
pick (SELL_NOW only if strictly lowest risk); ₹ amounts filled from tool facts; explainability.
cattle_count = 0 (or unknown) -> not applicable, no LLM call.
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from core import settings
from core.safety_floor import apply_confidence_cap, downgrade_all
from core.schemas import (CONFIDENCE_ORDER, Driver, Explain, F4AssessorOutput, F4Result, F4ResultOption, Money,
                          TraceStep)
from core.verifier import DEVANAGARI_DIGITS, numbers_in, verify
from features import explain
from features.f3_cropmix import inject_error
from orchestrators.base import Engine, EngineTask, FactStore, load_prompt, make_tools
from repo.base import Repo, get_repo
from tools import livestock
from tools.household import household_facts, load_household

FEATURE = "f4"
ROOT = Path(__file__).resolve().parent.parent
MONEYLENDER = re.compile(r"money ?lender|sahukar|sawkar|सावकार|साहूकार|साहुकार", re.IGNORECASE)
BORROW = re.compile(r"borrow|take (a )?loan|loan from|उधार|कर्ज घ्या|कर्ज काढ|कर्ज ले|कर्ज़ ले", re.IGNORECASE)


@lru_cache(maxsize=1)
def support_options() -> dict:
    return yaml.safe_load((ROOT / "config" / "support_options.yaml").read_text())


@dataclass
class F4Context:
    hh: dict
    village: dict
    repo: Repo

    @property
    def hh_id(self) -> str:
        return self.hh["hh_id"]

    @property
    def options(self) -> list[str]:
        return livestock.option_set(self.hh["cattle_count"])

    @property
    def context_bucket(self) -> str:
        return f"livestock|cash:{self.hh['cash_buffer_band'] or 'unknown'}"

    def required_calls(self) -> list[tuple[str, tuple]]:
        return [("get_livestock_facts", (self.hh_id,)), ("get_rain_status", (self.hh["village_id"],)),
                ("find_similar_households", (self.hh_id,))]


def build_context(hh_id: str, repo: Repo | None = None) -> F4Context:
    repo = repo or get_repo()
    hh, village = load_household(hh_id, repo)
    return F4Context(hh=hh, village=village, repo=repo)


def household_summary(ctx: F4Context) -> str:
    h = ctx.hh
    return json.dumps({k: h.get(k) for k in ("hh_id", "village_id", "cattle_count", "goats_count", "fodder_weeks_left",
                                             "cash_buffer_band", "off_farm_income", "irrigation")},
                      ensure_ascii=False, default=str)


def planner_rules(ctx: F4Context) -> str:
    return "\n".join([f"- Always call get_livestock_facts(hh_id='{ctx.hh_id}').",
                      f"- Always call get_rain_status(village_id={ctx.hh['village_id']}).",
                      f"- Always call find_similar_households(hh_id='{ctx.hh_id}').",
                      "- Do not call get_prices, get_calibration or get_crop_health (not needed for livestock)."])


def support_text() -> str:
    return "\n".join(f"- {k}: {v['name_en']} ({v['what']}) confirm with: {v['confirm_with']}"
                     for k, v in support_options().items())


def make_task(ctx: F4Context, store: FactStore, attempt: int, feedback: list[str] | None,
              style: str = "") -> EngineTask:
    summary = household_summary(ctx)
    fb = ("\nYOUR PREVIOUS ANSWER FAILED VERIFICATION. Fix these issues:\n- " + "\n- ".join(feedback) + "\n") if feedback else ""

    def assessor_prompt(facts_json: str) -> str:
        return load_prompt("f4_assessor", HOUSEHOLD=summary, OPTIONS=", ".join(ctx.options), FACTS=facts_json,
                           SUPPORT=support_text(), STYLE=style, FEEDBACK=fb)

    def verifier_prompt(facts_json: str, assessment_json: str) -> str:
        return load_prompt("verifier", FACTS=facts_json, ASSESSMENT=assessment_json)

    planner = load_prompt("planner", FEATURE="F4 livestock and fodder decision", HOUSEHOLD=summary,
                          RULES=planner_rules(ctx)) if attempt == 1 else None
    return EngineTask(feature=FEATURE, planner_prompt=planner, assessor_prompt=assessor_prompt,
                      verifier_prompt=verifier_prompt if settings.verifier_agent() else None,
                      tools=make_tools(store, ctx.repo), store=store, attempt=attempt,
                      assessor_schema=F4AssessorOutput)


def call_missing_tools(ctx: F4Context, store: FactStore) -> list[str]:
    fns = {fn.__name__: fn for fn in make_tools(store, ctx.repo)}
    done = {(c["tool"], tuple(map(str, c["args"]))) for c in store.calls}
    missing = []
    for tool, args in ctx.required_calls():
        if (tool, tuple(map(str, args))) not in done:
            fns[tool](*args)
            missing.append(f"{tool}({', '.join(map(str, args))})")
    return missing


def must_cite(ctx: F4Context, store: FactStore) -> dict[str, list[str]]:
    return {o: [f"F-f4-opt-{o}-cash"] for o in ctx.options if f"F-f4-opt-{o}-cash" in store.facts}


def all_texts(a: F4AssessorOutput) -> list[str]:
    return ([d.text for o in a.options for d in o.drivers] + [d.text for d in a.why_recommended]
            + [w.text for w in a.why_not_others] + [a.explanation_mr, a.explanation_hi])


def f4_rule_issues(a: F4AssessorOutput, store: FactStore) -> list[str]:
    """Code-enforced F4 rules (beyond the generic verifier)."""
    issues = []
    for text in all_texts(a):
        if MONEYLENDER.search(text) and BORROW.search(text):
            issues.append(f"never suggest borrowing from a moneylender: '{text[:80]}'")
    known = set(support_options())
    for o in a.options:
        bad = [s for s in o.support_ids if s not in known]
        if bad:
            issues.append(f"{o.name}: support_ids {bad} are not in config/support_options.yaml {sorted(known)}")
    # Every sentence that uses the fodder camp rate (cites F-f4-camp or quotes its number) must say it is a 2019 rate.
    camp = store.facts.get("F-f4-camp") if store is not None else None
    camp_value = camp.get("value") if camp else 100
    texts = [(d.text, d.fact_id) for o in a.options for d in o.drivers] + [(d.text, d.fact_id) for d in a.why_recommended] \
        + [(w.text, w.fact_id) for w in a.why_not_others] + [(a.explanation_mr, None), (a.explanation_hi, None)]
    for text, fid in texts:
        uses_camp = fid == "F-f4-camp" or (fid is None and ("छावणी" in text or "छावनी" in text or "camp" in text.lower())
                                          and any(abs(n - camp_value) < 0.5 for n in numbers_in(text)))
        if uses_camp and "2019" not in (text or "").translate(DEVANAGARI_DIGITS):
            issues.append(f"fodder camp rate used without '2019 rate; current rate not confirmed': '{text[:70]}'")
    return issues


def choose_recommended(a: F4AssessorOutput, order: list[str]) -> str:
    """Lowest risk wins (then higher confidence, then option order). SELL_NOW is recommended only when its
    risk is strictly lower than every other option's."""
    def key(o):
        return (o.risk, -CONFIDENCE_ORDER.index(o.confidence), order.index(o.name) if o.name in order else 99)
    others = [o for o in a.options if o.name != "SELL_NOW"]
    sell = next((o for o in a.options if o.name == "SELL_NOW"), None)
    best_other = min(others, key=key) if others else None
    if sell and (best_other is None or sell.risk < best_other.risk):
        return sell.name
    return best_other.name


def what_would_change(ctx: F4Context, store: FactStore) -> dict:
    """Nearest fact threshold, computed in code: fodder weeks at which buying fodder fits the cash-band limit."""
    t = settings.thresholds()["f4"]
    weeks = ctx.hh["fodder_weeks_left"]
    animals = ctx.hh["cattle_count"]
    band = ctx.hh["cash_buffer_band"] or "low"
    limit = t["cash_band_limit_rs"][band]
    horizon, intake = float(t["fodder_horizon_weeks"]), float(t["intake_kg_per_day_large"])
    price = store.facts["F-f4-kadba-now"]["value"]
    if weeks is None:
        text = "fodder_weeks_left is unknown: record it and the fodder cost and this comparison can be computed"
        fact = {"fact_id": "F-f4-wwc", "name": "what would change", "value": None, "unit": "weeks", "text": text,
                "source": "computed in code", "source_url": None, "is_synthetic": True}
        store.add([fact])
        return {"fact_id": "F-f4-wwc", "current": None, "threshold": None, "text": text}
    w_star = livestock.weeks_needed_for_limit(animals, intake, horizon, price, limit)
    if weeks >= w_star:
        text = (f"Buying fodder for the remaining {horizon - weeks:g} weeks already fits the {band} cash-band limit of "
                f"₹{limit:,} (assumption) at ₹{price:g}/kg")
    else:
        text = (f"If fodder_weeks_left were {w_star} or more (now {weeks:g}), buying kadba for the rest of the "
                f"{horizon:g}-week horizon would cost at most ₹{limit:,} (the {band} cash-band limit, an assumption) at "
                f"₹{price:g}/kg, so KEEP_AND_BUY_FODDER would need much less cash")
    store.add([{"fact_id": "F-f4-wwc", "name": "what would change", "value": w_star, "unit": "weeks", "text": text,
                "source": "computed in code from F-f4-kadba-now, config/thresholds.yaml f4", "source_url": None,
                "is_synthetic": True, "range": [w_star, limit]}])
    return {"fact_id": "F-f4-wwc", "current": weeks, "threshold": w_star, "limit_rs": limit, "text": text}


def money_from(store: FactStore, fid: str) -> Money:
    f = store.facts.get(fid)
    if not f:
        return Money()
    rng = f.get("range") or [None, None]
    return Money(value=f.get("value"), low=rng[0], high=rng[1], fact_id=fid)


def _not_applicable(ctx: F4Context, reason: str) -> F4Result:
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    return F4Result(hh_id=ctx.hh_id, applicable=False, reason=reason, verifier_status="NA", facts=store.as_list(),
                    orchestrator_used="code", context_bucket=ctx.context_bucket,
                    trace=[TraceStep(step="engine", backend="code", title=f"F4 not applicable: {reason} (no LLM call)")])


async def run_with_engine(engine: Engine, ctx: F4Context, inject: str | None = None, style: str = "",
                          rated_examples: int = 0) -> F4Result:
    if ctx.hh["cattle_count"] is None:
        return _not_applicable(ctx, "cattle count unknown (record it in the F1 interview)")
    if ctx.hh["cattle_count"] == 0:
        return _not_applicable(ctx, "no large animals")
    from features import learning

    if not style:
        style, rated_examples = learning.style_block(ctx.repo, FEATURE, ctx.context_bucket)
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    trace = [TraceStep(step="engine", title=f"Engine: {engine.name}")]

    out = await engine.run(make_task(ctx, store, 1, None, style))
    trace += out.trace
    missing = call_missing_tools(ctx, store)
    if missing:
        trace.append(TraceStep(step="planner", title="🧭 Required tools called by code", detail={"missing": missing}))
    wwc = what_would_change(ctx, store)
    trace.append(TraceStep(step="facts", title=f"🛰️ {len(store.facts)} facts",
                           detail={"facts": [{k: f.get(k) for k in ("fact_id", "text", "source", "source_url", "is_synthetic")}
                                             for f in store.as_list()]}))
    a, verdict = out.assessment, out.verdict
    if inject:
        trace.append(TraceStep(step="inject", title="Injected error (demo)", detail={"change": inject_error(a, inject)}))
    feedback = [f"facts added after the first answer, use them: {', '.join(missing)}"] if missing else []
    status, attempt = "FAIL", 1
    while True:
        issues = verify(a, store.facts, ctx.options, must_cite(ctx, store), risk_field="risk", check_why=True,
                        check_explanations=True) + f4_rule_issues(a, store)
        agent_issues = verdict.issues if verdict and verdict.status == "FAIL" else []
        trace.append(TraceStep(step="verifier_code", title="✅ Code verifier: PASS" if not issues else "❌ Code verifier: FAIL",
                               detail={"attempt": attempt, "issues": issues}))
        if not issues and not agent_issues and not feedback:
            status = "PASS"
            break
        if attempt > settings.max_regenerations():
            break
        attempt += 1
        out = await engine.run(make_task(ctx, store, attempt, feedback + issues + agent_issues, style))
        trace += out.trace
        a, verdict, feedback = out.assessment, out.verdict, []

    notes = []
    if status == "FAIL":
        notes += downgrade_all(a, "verifier failed after regeneration")
    completeness = store.facts["F-hh-completeness"]
    if "incomplete" in completeness.get("flags", []):
        notes += apply_confidence_cap(a, "LOW", "profile incomplete")
    if ctx.hh["fodder_weeks_left"] is None:
        notes += apply_confidence_cap(a, "LOW", "missing data: fodder weeks left unknown, fodder cost not computed")
    if notes:
        trace.append(TraceStep(step="calibration", title="Confidence lowered", detail={"changes": notes}))
    notes.append("calibration: no outcome history for livestock decisions yet (no calibration rule applied)")
    picked = choose_recommended(a, ctx.options)
    if picked != a.recommended:
        notes.append(f"Recommended set to {picked} (lowest risk; SELL_NOW only if strictly lowest); assessor had picked {a.recommended}")
    trace.append(TraceStep(step="safety", title=f"Recommended: {picked}",
                           detail={"rule": "SELL_NOW only if its risk is strictly lower than every other option",
                                   "risks": {o.name: o.risk for o in a.options}}))
    options = [F4ResultOption(**o.model_dump(), cash_needed=money_from(store, f"F-f4-opt-{o.name}-cash"),
                              asset_value_kept=money_from(store, f"F-f4-opt-{o.name}-asset"),
                              sale_proceeds=money_from(store, f"F-f4-opt-{o.name}-cash-in")
                              if f"F-f4-opt-{o.name}-cash-in" in store.facts else None) for o in a.options]
    ev = explain.evidence(store, rated_examples)
    trace.append(TraceStep(step="evidence", backend="code", title=ev["text"], detail=ev))
    return F4Result(
        withheld=status == "FAIL",
        withheld_reason="verification failed after regeneration; options shown for review only" if status == "FAIL" else None,
        hh_id=ctx.hh_id, options=options, recommended=picked, verifier_status=status, facts=store.as_list(),
        trace=trace, orchestrator_used=engine.name, notes=notes, context_bucket=ctx.context_bucket,
        explain=Explain(why_recommended=a.why_recommended, why_not_others=a.why_not_others, what_would_change=wwc,
                        confidence_reason=explain.confidence_reason(notes, status),
                        data_gaps=explain.data_gaps(store, explain.cited_ids(a)),
                        plain_explanation_mr=a.explanation_mr, plain_explanation_hi=a.explanation_hi, evidence=ev))


def rule_result(ctx: F4Context) -> F4Result:
    """No Gemini: facts in code, every option LOW with no risk judgement and no recommendation."""
    if not ctx.hh["cattle_count"]:
        return _not_applicable(ctx, "no large animals" if ctx.hh["cattle_count"] == 0 else "cattle count unknown")
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    store.call("get_livestock_facts", livestock.get_livestock_facts, ctx.hh_id, repo=ctx.repo)
    options = [F4ResultOption(name=o, cash_pressure="high" if ctx.hh["cash_buffer_band"] in ("low", None) else "med",
                              risk=0, confidence="LOW", support_ids=[],
                              drivers=[Driver(text="Cash needed for this option", fact_id=f"F-f4-opt-{o}-cash")],
                              cash_needed=money_from(store, f"F-f4-opt-{o}-cash"),
                              asset_value_kept=money_from(store, f"F-f4-opt-{o}-asset")) for o in ctx.options]
    return F4Result(hh_id=ctx.hh_id, options=options, recommended=None, verifier_status="RULE", facts=store.as_list(),
                    orchestrator_used="rule", context_bucket=ctx.context_bucket,
                    notes=["Rule result: AI unavailable; compare cash needed and value kept; risk not estimated"],
                    trace=[TraceStep(step="engine", backend="code", title="Engine: rule fallback (no AI)")],
                    explain=Explain(confidence_reason=["rule result: AI unavailable, confidence LOW"]))
