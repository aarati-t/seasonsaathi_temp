"""F2 Season Risk Watch (SPEC section 6, local alerts now, Pub/Sub in P6).

Per plot: stress facts + level in CODE (tools/stress.py) -> dedupe check (same plot + level within 7 days:
no LLM call) -> LOW: no alert, no LLM -> MED/HIGH: planner agent picks next_step, alert writer writes 2 lines
in Marathi + Hindi citing fact_ids and "rain data up to <date>" -> code verifier (fact_ids exist, numbers match
facts, date present; REQUEST_DRONE only when HIGH) -> one regeneration, then a code-built template alert ->
publish through services/alerts (LocalAlerts inserts one row).
"""
import json
import uuid
from dataclasses import dataclass

from core import settings
from core.schemas import F2Alert, F2Plan, F2Result, TraceStep
from core.verifier import DEVANAGARI_DIGITS, check_texts
from orchestrators.base import AgentSpec, Engine, load_prompt
from repo.base import Repo, get_repo
from services import alerts
from tools.stress import get_plot_stress_facts, stress_level

FEATURE = "f2"


@dataclass
class F2Context:
    plot: dict
    hh: dict
    repo: Repo

    @property
    def plot_id(self) -> str:
        return self.plot["plot_id"]


def build_context(plot_id: str, repo: Repo | None = None) -> F2Context:
    repo = repo or get_repo()
    plots = repo.fetch_where("plots", plot_id=plot_id)
    if not plots:
        raise KeyError(f"plot {plot_id} not found")
    hh = repo.fetch_where("households", hh_id=plots[0]["hh_id"])[0]
    return F2Context(plot=plots[0], hh=hh, repo=repo)


def _facts_json(facts: list[dict]) -> str:
    return json.dumps([{k: f.get(k) for k in ("fact_id", "text", "value", "unit", "source", "is_synthetic")} for f in facts],
                      ensure_ascii=False, default=str)


def alert_issues(alert: F2Alert, facts: list[dict], as_of: str | None) -> list[str]:
    """Code verifier for the alert text."""
    by_id = {f["fact_id"]: f for f in facts}
    issues = []
    if not alert.fact_ids:
        issues.append("alert must cite at least one fact_id")
    issues += [f"unknown fact_id {fid}" for fid in alert.fact_ids if fid not in by_id]
    issues += check_texts({"text_mr": alert.text_mr, "text_hi": alert.text_hi}, by_id)
    if as_of:
        for name, text in (("text_mr", alert.text_mr), ("text_hi", alert.text_hi)):
            if as_of not in (text or "").translate(DEVANAGARI_DIGITS):
                issues.append(f"{name} must state the rain data date {as_of}")
    return issues


def template_alert(level: str, step: str, facts: list[dict], as_of: str | None) -> F2Alert:
    """Code-built alert (no Gemini): used when the writer fails twice or AI is unavailable."""
    v = {f["fact_id"]: f.get("value") for f in facts}
    d, s = v.get("F-stress-deficit"), v.get("F-stress-dryspell")
    mr1 = f"ताण {level}: पाऊस {d}% कमी, सलग कोरडे दिवस {s}." if d is not None else f"ताण {level}."
    hi1 = f"तनाव {level}: बारिश {d}% कम, लगातार सूखे दिन {s}." if d is not None else f"तनाव {level}."
    return F2Alert(text_mr=f"{mr1}\nपुढील पाऊल: {step}; पावसाची माहिती {as_of} पर्यंत.",
                   text_hi=f"{hi1}\nअगला कदम: {step}; बारिश का डेटा {as_of} तक.",
                   fact_ids=[f for f in ("F-stress-deficit", "F-stress-dryspell", "F-stress-asof") if f in v])


async def run_with_engine(engine: Engine | None, ctx: F2Context, inject: str | None = None) -> F2Result:
    facts = get_plot_stress_facts(ctx.plot_id, ctx.repo)
    level, crossed = stress_level(facts)
    as_of = next((f["value"] for f in facts if f["fact_id"] == "F-stress-asof"), None)
    base = dict(plot_id=ctx.plot_id, hh_id=ctx.hh["hh_id"], stress_level=level, crossed=crossed, facts=facts)
    trace = [TraceStep(step="stress", backend="code", title=f"📈 Stress {level} ({len(crossed)} threshold(s) crossed)",
                       detail={"crossed": crossed, "facts": [f["text"] for f in facts]})]
    previous = alerts.recent_alert(ctx.repo, ctx.plot_id, level)
    if previous:
        trace.append(TraceStep(step="dedupe", backend="code", title="⏭️ Duplicate: same plot + level alerted within "
                               f"{settings.thresholds()['alerts']['dedupe_days']} days (no LLM call)",
                               detail={"alert_id": previous["alert_id"], "created_at": str(previous["created_at"])}))
        return F2Result(**base, deduped=True, alert={"alert_id": previous["alert_id"]}, next_step=previous.get("next_step"),
                        trace=trace, orchestrator_used="code", verifier_status="NA")
    if level == "LOW":
        trace.append(TraceStep(step="planner", backend="code", title="🧭 LOW: WAIT_NEXT_PASS, no alert (no LLM call)"))
        return F2Result(**base, next_step="WAIT_NEXT_PASS", trace=trace, orchestrator_used="code", verifier_status="NA")
    if engine is None:
        raise RuntimeError("no engine")

    plot_desc = json.dumps({"plot_id": ctx.plot_id, "hh_id": ctx.hh["hh_id"], "plot_location": "SYNTHETIC"
                            if ctx.plot["is_synthetic"] else "recorded"}, ensure_ascii=False)
    fj = _facts_json(facts)
    planner = AgentSpec(name="planner", output_schema=F2Plan, output_key="plan", title="🧭 Planner",
                        instruction=lambda state: load_prompt("f2_planner", PLOT=plot_desc, LEVEL=level,
                                                              CROSSED="; ".join(crossed), FACTS=fj))

    def writer(feedback: str = "") -> AgentSpec:
        return AgentSpec(name="writer", output_schema=F2Alert, output_key="alert", title="✍️ Alert writer",
                         instruction=lambda state: load_prompt("f2_alert", PLOT=plot_desc, LEVEL=level, FACTS=fj,
                                                               NEXT_STEP=state.get("plan", {}).get("next_step", "ALERT_AGENT"),
                                                               AS_OF=str(as_of), FEEDBACK=feedback))

    outputs, steps = await engine.run_agents(FEATURE, [planner, writer()], {})
    trace += steps
    plan: F2Plan = outputs["plan"]
    notes = []
    step = plan.next_step
    if step == "REQUEST_DRONE" and level != "HIGH":
        step = "REQUEST_PHOTOS"
        notes.append(f"REQUEST_DRONE is only allowed when stress is HIGH (level {level}): changed to REQUEST_PHOTOS")
    alert, status = outputs["alert"], "PASS"
    if inject == "bad_number":
        alert.text_mr += " (पाऊस 99% कमी)"
        trace.append(TraceStep(step="inject", title="Injected error (demo)", detail={"change": "added 99% to text_mr"}))
    issues = alert_issues(alert, facts, as_of)
    trace.append(TraceStep(step="verifier_code", backend="code", title="✅ Alert check: PASS" if not issues else "❌ Alert check: FAIL",
                           detail={"attempt": 1, "issues": issues}))
    if issues:
        fb = "YOUR PREVIOUS ALERT FAILED CHECKS. Fix:\n- " + "\n- ".join(issues)
        retry, steps = await engine.run_agents(FEATURE, [writer(fb)], {"plan": plan.model_dump()})
        trace += steps
        alert = retry["alert"]
        issues = alert_issues(alert, facts, as_of)
        trace.append(TraceStep(step="verifier_code", backend="code", title="✅ Alert check: PASS" if not issues else "❌ Alert check: FAIL",
                               detail={"attempt": 2, "issues": issues}))
        if issues:
            alert, status = template_alert(level, step, facts, as_of), "FAIL"
            notes.append("alert writer failed checks twice: code-built template alert used")
    return _publish(ctx, base, alert, step, status, as_of, trace, notes, engine.name)


def _publish(ctx, base, alert: F2Alert, step, status, as_of, trace, notes, orchestrator) -> F2Result:
    row = {"alert_id": f"AL-{uuid.uuid4().hex[:12]}", "plot_id": ctx.plot_id, "hh_id": ctx.hh["hh_id"],
           "stress_level": base["stress_level"], "next_step": step, "text_mr": alert.text_mr, "text_hi": alert.text_hi,
           "fact_ids": alert.fact_ids, "data_as_of": as_of}
    sink = alerts.get_sink(ctx.repo)
    stored = sink.publish(row)
    trace.append(TraceStep(step="publish", backend="code",
                           title=f"📣 Alert {row['alert_id']} via {sink.via}"
                                 + (f" (message {stored.get('message_id')}; stored when the push arrives)" if sink.via == "pubsub" else ""),
                           detail={"alert_id": row["alert_id"], "via": sink.via, "message_id": stored.get("message_id")}))
    return F2Result(**base, next_step=step, alert={**row, "created_at": stored.get("created_at"), "via": sink.via},
                    trace=trace, orchestrator_used=orchestrator, verifier_status=status, notes=notes)


def rule_result(ctx: F2Context) -> F2Result:
    """No Gemini: level in code, next step by rule, template alert."""
    facts = get_plot_stress_facts(ctx.plot_id, ctx.repo)
    level, crossed = stress_level(facts)
    as_of = next((f["value"] for f in facts if f["fact_id"] == "F-stress-asof"), None)
    base = dict(plot_id=ctx.plot_id, hh_id=ctx.hh["hh_id"], stress_level=level, crossed=crossed, facts=facts)
    trace = [TraceStep(step="engine", backend="code", title="Engine: rule fallback (no AI)")]
    if level == "LOW" or alerts.recent_alert(ctx.repo, ctx.plot_id, level):
        return F2Result(**base, next_step="WAIT_NEXT_PASS", trace=trace, orchestrator_used="rule", verifier_status="RULE",
                        deduped=level != "LOW")
    step = "ALERT_AGENT" if level == "HIGH" else "REQUEST_PHOTOS"
    return _publish(ctx, base, template_alert(level, step, facts, as_of), step, "RULE", as_of, trace,
                    ["Rule result: AI unavailable; template alert"], "rule")


async def scan(repo: Repo | None = None, engine: str | None = None, engines=None) -> dict:
    """Run F2 for every plot. Households without plots are skipped with reason 'no plot location'."""
    from orchestrators import selector

    repo = repo or get_repo()
    plots = {}
    for p in repo.fetch_all("plots"):
        plots.setdefault(p["hh_id"], []).append(p["plot_id"])
    summary = {"plots": 0, "HIGH": 0, "MED": 0, "LOW": 0, "alerts_created": 0, "deduped": 0, "skipped": []}
    results = []
    for hh in sorted(repo.fetch_all("households"), key=lambda h: h["hh_id"]):
        if hh["hh_id"] not in plots:
            summary["skipped"].append({"hh_id": hh["hh_id"], "reason": "no plot location"})
            continue
        for plot_id in plots[hh["hh_id"]]:
            r = await selector.execute(__import__(__name__, fromlist=["x"]), build_context(plot_id, repo), FEATURE,
                                       hh["hh_id"], engine=engine, repo=repo, engines=engines, use_result_cache=False)
            summary["plots"] += 1
            summary[r.stress_level] += 1
            summary["alerts_created"] += bool(r.alert and not r.deduped)
            summary["deduped"] += r.deduped
            results.append({"plot_id": plot_id, "hh_id": hh["hh_id"], "stress_level": r.stress_level,
                            "next_step": r.next_step, "deduped": r.deduped,
                            "alert_id": (r.alert or {}).get("alert_id"), "engine": r.orchestrator_used})
    return {"summary": summary, "results": results}
