"""F3 Water and Cash Crop-Mix Plan (rabi), SPEC section 6.

Flow per engine: planner (tools) -> assessor -> verifier agent, then in code: required-tool check,
code verifier, one regeneration on FAIL (then downgrade to LOW), calibration cap, safety floor.
Engine choice and fallback are in orchestrators/selector.py.
"""
import json
from dataclasses import dataclass

from core import settings
from core.safety_floor import (SIGNAL_RANK, apply_confidence_cap, apply_safety_floor, downgrade_all, lower_all_one_level,
                               lower_one)
from core.schemas import CONFIDENCE_ORDER, AssessorOutput, Driver, Explain, FeatureResult, Option, TraceStep
from core.verifier import numbers_in, verify
from features import explain
from orchestrators.base import Engine, EngineTask, FactStore, load_prompt, make_tools
from repo.base import Repo, get_repo
from tools import calibration, peers, prices, rain, water_budget
from tools.household import household_facts, load_household

FEATURE = "f3"
# Option name -> commodity keys for get_prices (fodder has no mandi price).
CROP_MIXES = {"Chana only": ["gram"], "Chana + safflower": ["gram", "safflower"], "Jowar + fodder": ["jowar"]}
# Option name -> canonical crop key (config/terms.yaml names) used for F7 calibration contexts.
CROP_KEYS = {"Chana only": "gram", "Chana + safflower": "gram+safflower", "Jowar + fodder": "sorghum+fodder"}
INJECT_MODES = ("bad_fact_id", "bad_number")


@dataclass
class F3Context:
    hh: dict
    village: dict
    repo: Repo
    rain_deficit: float | None = None   # this season's cumulative deficit % (CHIRPS), for calibration buckets

    @property
    def hh_id(self) -> str:
        return self.hh["hh_id"]

    @property
    def district(self) -> str:
        return self.village["district"]

    @property
    def rain_bucket(self) -> str:
        return calibration.rain_bucket(self.rain_deficit)

    @property
    def option_contexts(self) -> dict[str, str]:
        """F7 calibration context per option: rain bucket x irrigation x crop key."""
        return {name: calibration.context(self.rain_bucket, self.hh["irrigation"], key) for name, key in CROP_KEYS.items()}

    @property
    def context_bucket(self) -> str:
        return f"{self.rain_bucket}|{self.hh['irrigation'] or 'unknown'}"

    @property
    def needs_prices(self) -> bool:
        return self.hh["cash_buffer_band"] == "low"

    def required_calls(self) -> list[tuple[str, tuple]]:
        calls = [("get_rain_status", (self.hh["village_id"],)), ("find_similar_households", (self.hh_id,)),
                 ("get_soil_status", (self.hh_id,)), ("get_water_budget", (self.hh_id,))]
        calls += [("get_calibration", (c,)) for c in dict.fromkeys(self.option_contexts.values())]
        if self.needs_prices:
            calls += [("get_prices", (c, self.district)) for c in sorted({c for v in CROP_MIXES.values() for c in v})]
        return calls


def build_context(hh_id: str, repo: Repo | None = None) -> F3Context:
    repo = repo or get_repo()
    hh, village = load_household(hh_id, repo)
    deficit = next((f["value"] for f in rain.get_rain_status(hh["village_id"], repo) if f["fact_id"] == "F-rain-deficit"), None)
    return F3Context(hh=hh, village=village, repo=repo, rain_deficit=deficit)


def household_summary(ctx: F3Context) -> str:
    h = ctx.hh
    return json.dumps({"hh_id": ctx.hh_id, "village_id": h["village_id"], "district": ctx.district,
                       "irrigation": h["irrigation"], "water_hours_per_week": h["water_hours_per_week"],
                       "cash_buffer_band": h["cash_buffer_band"], "calibration_contexts": ctx.option_contexts},
                      ensure_ascii=False, default=str)


def planner_rules(ctx: F3Context) -> str:
    lines = [f"- Always call get_rain_status(village_id={ctx.hh['village_id']}).",
             f"- Always call find_similar_households(hh_id='{ctx.hh_id}').",
             f"- Always call get_soil_status(hh_id='{ctx.hh_id}') and get_water_budget(hh_id='{ctx.hh_id}').",
             *[f"- Always call get_calibration(context='{c}') (for {n})." for n, c in ctx.option_contexts.items()],
             "- If cash_buffer_band is low, call get_prices(commodity, district) for each crop in the options: "
             f"gram, safflower, jowar, with district='{ctx.district}'. Otherwise do not call get_prices.",
             "- Do not call get_crop_health (not needed for rabi planning)."]
    return "\n".join(lines)


def msp_only_facts(store: FactStore) -> dict[str, str]:
    """commodity -> fact_id of an MSP-only price fact (no mandi data)."""
    out = {}
    for f in store.as_list():
        if f["fact_id"].startswith("F-price-") and f["fact_id"].endswith("-msp") and f.get("mandi_available") is False:
            out[f["fact_id"].split("-")[2]] = f["fact_id"]
    return out


def must_cite(ctx: F3Context, store: FactStore) -> dict[str, list[str]]:
    msp_only = msp_only_facts(store)
    out = {}
    for name, crops in CROP_MIXES.items():
        cal = calibration.fact_id_for(ctx.option_contexts[name])
        ids = [cal] if cal in store.facts else []
        wb = store.facts.get(water_budget.coverage_fact_id(name))
        if wb and wb.get("available"):
            ids.append(wb["fact_id"])
        ids += [msp_only[c] for c in crops if c in msp_only]
        out[name] = ids
    return out


def extra_rules(store: FactStore) -> str:
    msp_only = msp_only_facts(store)
    if not msp_only:
        return ""
    lines = []
    for crop, fid in msp_only.items():
        opts = [n for n, crops in CROP_MIXES.items() if crop in crops]
        lines.append(f"3b. {crop}: no current mandi price (see {fid}). For {', '.join(opts)}: lower the "
                     f"confidence by one level and add a driver citing {fid} that says the mandi price is unavailable.")
    return "\n".join(lines)


def cal_rule(ctx: F3Context) -> str:
    pairs = "; ".join(f"{n} -> {calibration.fact_id_for(c)}" for n, c in ctx.option_contexts.items())
    return ("Each option has its own calibration fact; every option cites it in one driver "
            f"({pairs}). Set confidence from the other evidence only: do NOT lower it because of calibration, "
            "code applies the calibration rule after you (HIGH -> MED when past HIGH calls there were often wrong).")


def make_task(ctx: F3Context, store: FactStore, attempt: int, feedback: list[str] | None, style: str = "") -> EngineTask:
    summary = household_summary(ctx)
    fb = ""
    if feedback:
        fb = "\nYOUR PREVIOUS ANSWER FAILED VERIFICATION. Fix these issues:\n- " + "\n- ".join(feedback) + "\n"

    def assessor_prompt(facts_json: str) -> str:
        return load_prompt("assessor", HOUSEHOLD=summary, OPTIONS=", ".join(CROP_MIXES), FACTS=facts_json,
                           CAL_RULE=cal_rule(ctx), EXTRA_RULES=extra_rules(store), FEEDBACK=fb, STYLE=style,
                           DEFICIT=str(settings.thresholds()["rain"]["deficit_pct_alert"]),
                           WB_FIT=str(settings.thresholds()["water_budget"]["fit_min_pct"]),
                           WB_TIGHT=str(settings.thresholds()["water_budget"]["tight_min_pct"]))

    def verifier_prompt(facts_json: str, assessment_json: str) -> str:
        return load_prompt("verifier", FACTS=facts_json, ASSESSMENT=assessment_json)

    planner = None
    if attempt == 1:
        planner = load_prompt("planner", FEATURE="F3 rabi crop-mix plan", HOUSEHOLD=summary, RULES=planner_rules(ctx))
    return EngineTask(feature=FEATURE, planner_prompt=planner, assessor_prompt=assessor_prompt,
                      verifier_prompt=verifier_prompt if settings.verifier_agent() else None,
                      tools=make_tools(store, ctx.repo), store=store, attempt=attempt)


def water_fit_issues(output: AssessorOutput, store: FactStore) -> list[str]:
    """P7: water_fit must agree with the option's coverage band (when coverage is available)."""
    issues = []
    for o in output.options:
        fact = store.facts.get(water_budget.coverage_fact_id(o.name)) if o.name in water_budget.OPTION_CROPS else None
        if not fact or not fact.get("available"):
            continue
        lo, hi = fact["range"]
        ok = water_budget.allowed_bands(lo, hi)
        if o.water_fit not in ok:
            issues.append(f"{o.name}: water_fit '{o.water_fit}' contradicts {fact['fact_id']} "
                          f"(covers {lo}-{hi}% of need -> {' or '.join(ok)})")
    return issues


def budget_short_options(store: FactStore) -> dict[str, str]:
    """Options whose code water budget is short over its whole range (safety floor input)."""
    out = {}
    for name in water_budget.OPTION_CROPS:
        f = store.facts.get(water_budget.coverage_fact_id(name))
        if f and f.get("available") and water_budget.band(f["range"][1]) == "short":
            out[name] = f"covers {f['range'][0]}-{f['range'][1]}% of need"
    return out


def lower_where_no_water_budget(output: AssessorOutput, store: FactStore) -> list[str]:
    """P7: an option whose water coverage could not be computed loses one confidence level (data gap)."""
    notes = []
    for o in output.options:
        fact = store.facts.get(water_budget.coverage_fact_id(o.name)) if o.name in water_budget.OPTION_CROPS else None
        if fact and not fact.get("available"):
            new = lower_one(o.confidence)
            if new != o.confidence:
                notes.append(f"Confidence lowered: {o.name} {o.confidence} -> {new} (water budget not available)")
                o.confidence = new
    return notes


def call_missing_tools(ctx: F3Context, store: FactStore) -> list[str]:
    """Required tools the planner skipped are called by code, so facts are never missing."""
    fns = {fn.__name__: fn for fn in make_tools(store, ctx.repo)}
    done = {(c["tool"], tuple(map(str, c["args"]))) for c in store.calls}
    missing = []
    for tool, args in ctx.required_calls():
        if (tool, tuple(map(str, args))) not in done:
            fns[tool](*args)
            missing.append(f"{tool}({', '.join(map(str, args))})")
    return missing


def inject_error(output: AssessorOutput, mode: str) -> str:
    """Demo (SPEC F3 acceptance): corrupt the first assessor output so the verifier must catch it."""
    d = output.options[0].drivers[0]
    if mode == "bad_fact_id":
        d.fact_id = "F-corrupted-999"
        return f"{output.options[0].name}: driver fact_id replaced with F-corrupted-999"
    nums = numbers_in(d.text)
    d.text = f"{d.text} (about {round(nums[0] * 3 + 37) if nums else 9999} units)"
    return f"{output.options[0].name}: added a number not in fact {d.fact_id}"


def choose_recommended(output: AssessorOutput) -> str:
    """Gemini scores each option; code picks from those scores so near-ties do not flip between runs:
    best signal (ACT > WAIT > AVOID), then higher confidence (categorical, stable across runs),
    then lowest failure_risk (drifts a few points run to run), then option order."""
    order = list(CROP_MIXES)
    best = min(output.options, key=lambda o: (-SIGNAL_RANK[o.signal], -CONFIDENCE_ORDER.index(o.confidence),
                                              o.failure_risk if o.failure_risk is not None else 101,
                                              order.index(o.name) if o.name in order else len(order)))
    return best.name


def _facts_step(store: FactStore) -> TraceStep:
    return TraceStep(step="facts", title=f"🛰️ {len(store.facts)} facts",
                     detail={"facts": [{k: f.get(k) for k in ("fact_id", "text", "source", "source_url", "is_synthetic")}
                                       for f in store.as_list()]})


def ndvi_unavailable_fact(ctx: F3Context) -> list[dict]:
    """Households saved from F1 have no plot (coordinates are never invented), so no NDVI."""
    if ctx.repo.fetch_where("plots", hh_id=ctx.hh_id):
        return []
    return [{"fact_id": "F-ndvi-unavailable", "value": None, "unit": None, "source": "plots table",
             "source_url": None, "is_synthetic": False,
             "text": "crop health (NDVI) unavailable for this household (no plot location on record)"}]


def what_would_change(ctx: F3Context, store: FactStore, safety: list[str], cal_details: list[dict]) -> dict:
    """Nearest threshold that would flip the result, computed in code (Gemini does not choose it).
    Priority: safety rule (rain-deficit rubric threshold) -> calibration downgrade (hit-rate threshold) ->
    incomplete profile (completeness threshold) -> NDVI unavailable -> rain-bucket boundary."""
    t = settings.thresholds()
    deficit = ctx.rain_deficit
    out = None
    if safety and deficit is not None and ctx.hh["irrigation"] in ("rainfed", None):
        thr = t["rain"]["deficit_pct_alert"]
        blocked = [n.split(":")[1].split(" changed")[0].strip() for n in safety]
        out = {"fact_id": "F-rain-deficit", "current": deficit, "threshold": thr,
               "text": f"If the season rain deficit were below {thr}% (now {deficit}%), a rainfed farm's water would not be "
                       f"rated short, so the safety rule would not block ACT for {', '.join(blocked)}"}
    elif any(d["result"] == "downgraded to MED" for d in cal_details):
        d = next(d for d in cal_details if d["result"] == "downgraded to MED")
        thr = round(t["calibration"]["high_min_hit_rate"] * 100)
        out = {"fact_id": d["fact_id"], "current": round(d["high_hit_rate"] * 100), "threshold": thr,
               "text": f"If past HIGH calls in {d['context']} were right {thr}% of the time or more (now "
                       f"{round(d['high_hit_rate'] * 100)}%, n={d['n']}), {d['option']} would keep HIGH confidence"}
    elif "incomplete" in store.facts["F-hh-completeness"].get("flags", []):
        thr = t["profile"]["min_completeness_pct"]
        cur = store.facts["F-hh-completeness"]["value"]
        missing = [f["text"].split(":")[0] for f in store.as_list() if f["fact_id"].startswith("F-hh-") and f.get("value") is None]
        out = {"fact_id": "F-hh-completeness", "current": cur, "threshold": thr,
               "text": f"If profile completeness reached {thr}% (now {cur}%), confidence would no longer be capped at LOW; "
                       f"missing: {', '.join(missing[:4])}"}
    elif "F-ndvi-unavailable" in store.facts:
        out = {"fact_id": "F-ndvi-unavailable", "current": None, "threshold": None,
               "text": "With a recorded plot location, crop health (NDVI) would be available and confidence would not be lowered one level"}
    elif deficit is not None:
        low, high = t["calibration"]["rain_buckets"]
        thr = high if deficit > high else low if deficit >= low else None
        if thr is not None:
            out = {"fact_id": "F-rain-deficit", "current": deficit, "threshold": thr,
                   "text": f"If the rain deficit fell below {thr}% (now {deficit}%), the calibration context would move to "
                           f"a drier-to-normal bucket and confidence could change"}
    if out is None:
        return {"fact_id": None, "current": None, "threshold": None, "text": "No nearby threshold changes this result"}
    store.add([{"fact_id": "F-f3-wwc", "name": "what would change", "value": out["threshold"], "unit": None,
                "text": out["text"], "source": "computed in code", "source_url": None, "is_synthetic": False,
                "range": [x for x in (out["current"], out["threshold"]) if isinstance(x, (int, float))]}])
    return out


async def run_with_engine(engine: Engine, ctx: F3Context, inject: str | None = None, style: str = "",
                          rated_examples: int = 0) -> FeatureResult:
    from features import learning

    if not style:
        style, rated_examples = learning.style_block(ctx.repo, FEATURE, ctx.context_bucket)
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    store.add(ndvi_unavailable_fact(ctx))
    trace = [TraceStep(step="engine", title=f"Engine: {engine.name}")]

    out = await engine.run(make_task(ctx, store, attempt=1, feedback=None, style=style))
    trace += out.trace
    missing = call_missing_tools(ctx, store)
    if missing:
        trace.append(TraceStep(step="planner", title="🧭 Required tools called by code",
                               detail={"missing": missing}))
    trace.append(_facts_step(store))

    assessment, verdict = out.assessment, out.verdict
    if inject:
        trace.append(TraceStep(step="inject", title="Injected error (demo)", detail={"change": inject_error(assessment, inject)}))
    feedback = [f"facts added after the first answer, use them: {', '.join(missing)}"] if missing else []
    status, attempt = "FAIL", 1
    while True:
        issues = verify(assessment, store.facts, list(CROP_MIXES), must_cite(ctx, store), check_why=True,
                        check_explanations=True) + water_fit_issues(assessment, store)
        agent_issues = verdict.issues if verdict and verdict.status == "FAIL" else []
        trace.append(TraceStep(step="verifier_code", title="✅ Code verifier: PASS" if not issues else "❌ Code verifier: FAIL",
                               detail={"attempt": attempt, "issues": issues}))
        if not issues and not agent_issues and not feedback:
            status = "PASS"
            break
        if attempt > settings.max_regenerations():
            break
        attempt += 1
        out = await engine.run(make_task(ctx, store, attempt=attempt, feedback=feedback + issues + agent_issues,
                                         style=style))
        trace += out.trace
        assessment, verdict, feedback = out.assessment, out.verdict, []

    notes = []
    if status == "FAIL":
        notes += downgrade_all(assessment, "verifier failed after regeneration")
    cal_notes, cal_details = calibration.apply_calibration_rule(assessment, ctx.option_contexts, store.facts)
    downgraded = [d for d in cal_details if d["result"] == "downgraded to MED"]
    trace.append(TraceStep(step="calibration", backend="code",
                           title=("📊 calibration downgrade: " + ", ".join(d["option"] for d in downgraded)) if downgraded
                           else "📊 Calibration: no downgrade",
                           detail={"rule": "HIGH and hit_rate < 0.70 and n >= 5 -> MED; n < 5 -> no downgrade",
                                   "options": cal_details, "changes": cal_notes,
                                   "text": "; ".join(cal_notes) or "no option downgraded"}))
    notes += cal_notes
    completeness = store.facts["F-hh-completeness"]
    if "incomplete" in completeness.get("flags", []):
        thin = apply_confidence_cap(assessment, "LOW", "profile incomplete")
        trace.append(TraceStep(step="calibration", title="Confidence lowered: profile incomplete",
                               detail={"fact_id": "F-hh-completeness", "text": completeness["text"], "changes": thin}))
        notes += thin
    if "F-ndvi-unavailable" in store.facts:
        no_ndvi = lower_all_one_level(assessment, "crop health (NDVI) unavailable for this household")
        trace.append(TraceStep(step="calibration", title="Confidence lowered: crop health (NDVI) unavailable",
                               detail={"fact_id": "F-ndvi-unavailable", "changes": no_ndvi,
                                       "text": store.facts["F-ndvi-unavailable"]["text"]}))
        notes += no_ndvi
    no_wb = lower_where_no_water_budget(assessment, store)
    if no_wb:
        trace.append(TraceStep(step="calibration", title="Confidence lowered: water budget not available",
                               detail={"changes": no_wb, "text": "; ".join(
                                   store.facts[water_budget.coverage_fact_id(n)]["text"] for n in water_budget.OPTION_CROPS
                                   if water_budget.coverage_fact_id(n) in store.facts)}))
        notes += no_wb
    safety = apply_safety_floor(assessment, ctx.hh["cash_buffer_band"], budget_short_options(store))
    trace.append(TraceStep(step="safety", title="Safety rule applied" if safety else "Safety rule not needed",
                           detail={"notes": safety}))
    picked = choose_recommended(assessment)
    if picked != assessment.recommended:
        notes.append(f"Recommended set to {picked} (best signal, then confidence, then lowest failure risk); "
                     f"assessor had picked {assessment.recommended}")
        assessment.recommended = picked
    all_notes = notes + safety
    wwc = what_would_change(ctx, store, safety, cal_details)
    ev = explain.evidence(store, rated_examples)
    trace.append(TraceStep(step="evidence", backend="code", title=ev["text"], detail=ev))
    withheld = status == "FAIL"
    return FeatureResult(hh_id=ctx.hh_id, options=assessment.options, recommended=assessment.recommended,
                         withheld=withheld,
                         withheld_reason="verification failed after regeneration; options shown for review only"
                         if withheld else None,
                         verifier_status=status, safety_rule_applied=bool(safety),
                         explanation_mr=assessment.explanation_mr, explanation_hi=assessment.explanation_hi,
                         facts=store.as_list(), trace=trace, orchestrator_used=engine.name, notes=all_notes,
                         context_bucket=ctx.context_bucket,
                         explain=Explain(why_recommended=assessment.why_recommended,
                                         why_not_others=assessment.why_not_others, what_would_change=wwc,
                                         confidence_reason=explain.confidence_reason(all_notes, status),
                                         data_gaps=explain.data_gaps(store, explain.cited_ids(assessment)),
                                         plain_explanation_mr=assessment.explanation_mr,
                                         plain_explanation_hi=assessment.explanation_hi, evidence=ev))


def rule_result(ctx: F3Context) -> FeatureResult:
    """Last fallback (SPEC rule 6): no Gemini. Plain rules on real facts, every option WAIT / LOW."""
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    store.call("get_rain_status", rain.get_rain_status, ctx.hh["village_id"], repo=ctx.repo)
    for c in dict.fromkeys(ctx.option_contexts.values()):
        store.call("get_calibration", calibration.get_calibration, c, repo=ctx.repo)
    deficit = store.facts.get("F-rain-deficit")
    rainfed = ctx.hh["irrigation"] in (None, "rainfed")
    dry = rainfed and deficit is not None and deficit["value"] > settings.thresholds()["rain"]["deficit_pct_alert"]
    water_fit = "short" if dry else "tight" if rainfed else "fit"
    cash = {"low": "high", "med": "med", "high": "low"}.get(ctx.hh["cash_buffer_band"], "med")
    drivers = [Driver(text="Water source on record", fact_id="F-hh-irrigation"),
               Driver(text="Cash buffer on record", fact_id="F-hh-cash_buffer_band")]
    if deficit:
        drivers.append(Driver(text="Season rainfall compared with normal", fact_id="F-rain-deficit"))
    options = [Option(name=n, signal="WAIT", failure_risk=None, drivers=drivers, water_fit=water_fit,
                      cash_at_risk=cash, confidence="LOW") for n in CROP_MIXES]
    return FeatureResult(
        hh_id=ctx.hh_id, options=options, recommended=next(iter(CROP_MIXES)), verifier_status="RULE",
        explanation_mr="AI सल्ला सध्या उपलब्ध नाही. हा फक्त नियमांवर आधारित प्राथमिक अंदाज आहे; कृपया नंतर पुन्हा तपासा.",
        explanation_hi="AI सलाह अभी उपलब्ध नहीं है. यह केवल नियमों पर आधारित प्रारंभिक अनुमान है; कृपया बाद में फिर देखें.",
        facts=store.as_list(), trace=[TraceStep(step="engine", title="Engine: rule fallback (no AI)")],
        orchestrator_used="rule", notes=["Rule result: AI unavailable; confidence LOW; failure risk not estimated"])
