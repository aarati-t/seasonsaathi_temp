"""P5b impact simulator: KPIs K1-K7 computed in CODE from cached, eligibility-checked F3/F4 results, stored data
and the stored verifier benchmark. Gemini never calculates a KPI.

Normal replay: reads impact_cache (keyed by household, feature, engine, model, fact snapshot hash and code/prompt/
config versions), makes no model calls, writes no outcomes, and appends one impact_runs row. Missing or stale cache
entries are REPORTED, never generated. refresh=True (CLI only) builds the missing entries and runs the K4 benchmark.
"""
import asyncio
import hashlib
import json
import os
import statistics
import time
import uuid
from datetime import date, datetime, timezone
from functools import lru_cache

import yaml

from core import llm, settings
from core.settings import ROOT
from features import f3_cropmix, f4_livestock, verifier_bench
from features.f3_cropmix import ndvi_unavailable_fact
from orchestrators.base import FactStore, make_tools
from tools import water_budget
from tools.calibration import outcome_provenance
from tools.household import household_facts
from tools.stress import get_plot_stress_facts, stress_level

MODULES = {"f3": f3_cropmix, "f4": f4_livestock}
STATUSES = {"SIMULATED", "TESTED", "MEASURED", "REPLAY", "TARGET", "NOT_ESTABLISHED", "NOT_RUN"}
VERSION_FILES = ["prompts/*.md", "config/thresholds.yaml", "config/support_options.yaml", "config/terms.yaml",
                 "config/soil_water.yaml", "config/commodities.yaml", "core/verifier.py", "core/safety_floor.py",
                 "core/schemas.py", "features/f3_cropmix.py", "features/f4_livestock.py", "features/explain.py",
                 "tools/*.py", "orchestrators/*.py"]


@lru_cache(maxsize=1)
def config() -> dict:
    return yaml.safe_load((ROOT / "config" / "impact.yaml").read_text())


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:24]


class ReadCache:
    """Read-only replay view: each table is read ONCE, then filtered in memory with the same case-insensitive text
    equality as Repo.fetch_where (one query per table on BigQuery instead of thousands). Never writes."""

    def __init__(self, repo):
        self.repo, self._tables = repo, {}

    def fetch_all(self, table):
        if table not in self._tables:
            self._tables[table] = self.repo.fetch_all(table)
        return [dict(r) for r in self._tables[table]]

    def fetch_where(self, table, **eq):
        want = {k: str(v).lower() for k, v in eq.items()}
        return [r for r in self.fetch_all(table) if all(str(r.get(k)).lower() == v for k, v in want.items())]


# ---------- population, versions, snapshots, cache keys ----------

def population(repo, which: str = "demo") -> list[dict]:
    rows = sorted(repo.fetch_all("households"), key=lambda h: h["hh_id"])
    return rows if which == "all" else [h for h in rows if h.get("is_synthetic")]


def population_snapshot_id(pop: list[dict]) -> str:
    return sha([{k: v for k, v in h.items() if k not in ("profile_embedding", "created_at")} for h in pop])


def version_hash() -> str:
    files = sorted({p for pattern in VERSION_FILES for p in ROOT.glob(pattern)})
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    h.update(json.dumps({"model": llm.gemini_model(), "verifier_agent": settings.verifier_agent(),
                         "thinking": settings.thinking(), "regen": settings.max_regenerations()}).encode())
    return h.hexdigest()[:24]


def fact_snapshot(feature: str, hh_id: str, repo) -> str:
    """Hash of every fact the feature would see (code tools only, no model) + the style examples it would use."""
    from features import learning
    module = MODULES[feature]
    ctx = module.build_context(hh_id, repo)
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    if feature == "f3":
        store.add(ndvi_unavailable_fact(ctx))
    fns = {fn.__name__: fn for fn in make_tools(store, repo)}
    for tool, args in ctx.required_calls():
        fns[tool](*args)
    style, _ = learning.style_block(repo, feature, ctx.context_bucket)
    return sha({"facts": sorted(store.as_list(), key=lambda f: f["fact_id"]), "style": style})


def cache_key(hh_id: str, feature: str, engine: str, model: str, snapshot: str, versions: str) -> str:
    return sha([hh_id, feature, engine, model, snapshot, versions])


def eligibility(result: dict | None) -> tuple[bool, str | None]:
    """P5b F: only verified, non-withheld, freshly assessed results feed recommendation KPIs."""
    if result is None:
        return False, "no cached assessment for this snapshot"
    if result.get("applicable") is False:
        return False, f"not applicable ({result.get('reason')})"
    if result.get("orchestrator_used") not in ("adk", "plain"):
        return False, f"fallback output ({result.get('orchestrator_used')}): no verified assessment for this snapshot"
    if result.get("verifier_status") != "PASS":
        return False, f"verifier status {result.get('verifier_status')}"
    if result.get("withheld"):
        return False, "recommendation withheld"
    agent = [t for t in result.get("trace", []) if t.get("step") == "verifier_agent"]
    if not agent:
        return False, "semantic verification not run (VERIFIER_AGENT off)"
    if (agent[-1].get("detail") or {}).get("status") != "PASS":
        return False, "semantic verifier did not pass"
    return True, None


# ---------- cache build (refresh) and lookup (replay) ----------

def wanted(pop: list[dict]) -> list[tuple[str, str]]:
    out = [(h["hh_id"], "f3") for h in pop]
    out += [(h["hh_id"], "f4") for h in pop if (h.get("cattle_count") or 0) >= 1]
    return out


def lookup(repo, pop, engine: str, versions: str) -> dict:
    model, rows = llm.gemini_model(), {}
    for r in repo.fetch_all("impact_cache"):
        if r["cache_key"] not in rows or str(r["created_at"]) > str(rows[r["cache_key"]]["created_at"]):
            rows[r["cache_key"]] = r
    out = {}
    for hh_id, feature in wanted(pop):
        snap = fact_snapshot(feature, hh_id, repo)
        key = cache_key(hh_id, feature, engine, model, snap, versions)
        row = rows.get(key)
        out[(hh_id, feature)] = {"key": key, "snapshot": snap,
                                 "result": json.loads(row["result_json"]) if row else None,
                                 "model_calls": row["model_calls"] if row else None,
                                 "latency_ms": row["latency_ms"] if row else None}
    return out


def refresh(repo, pop, engine: str, versions: str, log=print) -> dict:
    """CLI only: build missing cache entries (model calls). Existing entries for the same key are reused."""
    from orchestrators import selector
    found = lookup(repo, pop, engine, versions)
    built = calls = 0
    for (hh_id, feature), item in found.items():
        if item["result"] is not None:
            continue
        t0 = time.perf_counter()
        result = asyncio.run(selector.run(feature, hh_id, engine=engine, repo=repo))
        usage = next((t for t in reversed(result.trace) if t.step == "usage"), None)
        n_calls = sum(1 for c in (usage.detail.get("calls", []) if usage else []) if not c.get("cached"))
        repo.insert("impact_cache", [{
            "cache_key": item["key"], "hh_id": hh_id, "feature": feature, "engine": engine, "model": llm.gemini_model(),
            "snapshot_hash": item["snapshot"], "version_hash": versions, "result_json": result.model_dump_json(),
            "model_calls": n_calls, "latency_ms": round((time.perf_counter() - t0) * 1000),
            "created_at": datetime.now(timezone.utc).isoformat()}])
        built += 1
        calls += n_calls
        log(f"  built {feature} {hh_id}: {result.orchestrator_used} {result.verifier_status} ({n_calls} model calls)")
    return {"built": built, "model_calls": calls}


# ---------- KPI helpers ----------

def kpi(id_, name, value, unit, status, **kw) -> dict:
    assert status in STATUSES, status
    base = {"id": id_, "name": name, "value": value, "unit": unit, "status": status, "formula_text": "",
            "baseline_definition": None, "n_total": None, "n_eligible": None, "n_excluded": None,
            "exclusion_reasons": {}, "data_sources": [], "data_as_of": None, "provenance_counts": {},
            "assumptions": [], "limitations": [], "talking_point": "", "per_household_detail": []}
    return {**base, **kw}


def count_reasons(reasons: list[str]) -> dict:
    out = {}
    for r in reasons:
        out[r] = out.get(r, 0) + 1
    return out


def median(xs):
    return round(statistics.median(xs), 1) if xs else None


# ---------- K1 ----------

def money(m: dict | None, scenario: str):
    if m is None:
        return None
    return m.get({"low": "low", "mid": "value", "high": "high"}[scenario])


def position(option: dict, scenario: str):
    """sale proceeds + retained animal value - fodder expenditure over the F4 horizon. None if a ₹ input is missing.
    An option without a sale has proceeds 0 by definition (it sells nothing), not 'unknown'."""
    kept, fodder = money(option.get("asset_value_kept"), scenario), money(option.get("cash_needed"), scenario)
    proceeds = 0 if option.get("sale_proceeds") is None else money(option["sale_proceeds"], scenario)
    if kept is None or fodder is None or proceeds is None:
        return None
    return proceeds + kept - fodder


def k1(items: dict, pop: list[dict]) -> dict:
    cfg = config()["k1"]
    cattle = [h for h in pop if (h.get("cattle_count") or 0) >= 1]
    detail, reasons = [], []
    for h in cattle:
        r = items.get((h["hh_id"], "f4"), {}).get("result")
        ok, why = eligibility(r)
        if not ok:
            reasons.append(why)
            detail.append({"hh_id": h["hh_id"], "eligible": False, "reason": why})
            continue
        opts = {o["name"]: o for o in r["options"]}
        base, rec = opts.get(cfg["baseline_option"]), opts.get(r["recommended"])
        if base is None or rec is None:
            reasons.append("baseline or recommended option missing")
            detail.append({"hh_id": h["hh_id"], "eligible": False, "reason": "baseline or recommended option missing"})
            continue
        row = {"hh_id": h["hh_id"], "eligible": True, "cattle": h["cattle_count"], "recommended": r["recommended"],
               "combined_feasibility": "unknown"}
        for s in cfg["scenarios"]:
            pb, pr = position(base, s), position(rec, s)
            row[f"baseline_{s}"], row[f"option_{s}"] = pb, pr
            row[f"improvement_{s}"] = None if pb is None or pr is None else pr - pb
        if row["improvement_mid"] is None:
            reasons.append("₹ fact missing for an option")
            detail.append({"hh_id": h["hh_id"], "eligible": False, "reason": "₹ fact missing for an option"})
            continue
        detail.append(row)
    ok_rows = [d for d in detail if d["eligible"]]
    imp = [d["improvement_mid"] for d in ok_rows]
    value = round(statistics.mean(imp)) if imp else None
    scen = {}
    for s in cfg["scenarios"]:
        vals = [d[f"improvement_{s}"] for d in ok_rows if d[f"improvement_{s}"] is not None]
        scen[s] = {"mean": round(statistics.mean(vals)) if vals else None, "n": len(vals)}
    return kpi("K1", "Simulated net asset position improvement (livestock)", value, "₹ per eligible household",
               "SIMULATED" if imp else "NOT_ESTABLISHED", internal_key="livestock_net_benefit_rs",
               formula_text="position = sale proceeds + retained animal value − fodder expenditure over the F4 horizon; "
                            "improvement = recommended-option position − baseline position (per household, ₹ from F4 facts)",
               baseline_definition=f"{cfg['baseline_option']}: sell all large animals now at the sourced drought-price "
                                   "scenario (a scenario baseline, not the household's intended action)",
               n_total=len(cattle), n_eligible=len(ok_rows), n_excluded=len(cattle) - len(ok_rows),
               exclusion_reasons=count_reasons(reasons),
               mean=value, median=median(imp), total=sum(imp) if imp else None,
               n_better=sum(1 for x in imp if x > 0), scenarios=scen,
               combined_feasibility={"status": "unknown", "missing_inputs": ["crop input cost (F3 has no cost facts)",
                                                                             "household cash amount (only cash bands)"]},
               data_sources=["F4 facts: livestock_prices.csv (kadba price, drought sale drop, normal animal value)"],
               assumptions=["Normal animal value is SYNTHETIC (assumption rows in livestock_prices.csv)",
                            "low/high = every F4 price and cost range at its low/high end; mid = central value",
                            "Fodder horizon and intake from config/thresholds.yaml (f4)"],
               limitations=["Retained animal value is not realised cash or a guaranteed future sale value",
                            "Not modelled: milk income, other feed, care expenses, future animal-price changes",
                            "Standalone valuation scenario: not evidence that the household can afford the option "
                            "(combined F3+F4 feasibility unknown)",
                            "Demo population is synthetic"],
               talking_point=(f"For {len(ok_rows)} verified synthetic demo households, the recommended livestock option "
                              f"leaves a higher simulated asset position than selling everything now in "
                              f"{sum(1 for x in imp if x > 0)} cases (mean ₹{value:,})." if imp else
                              "Not established: no verified F4 assessments in the cache."),
               per_household_detail=detail)


# ---------- K2 ----------

def short_state(fact: dict | None) -> str | None:
    """'short' | 'not_short' from the code water budget; None when unknown or the range straddles the boundary."""
    if not fact or not fact.get("available"):
        return None
    lo, hi = fact["range"]
    tight = settings.thresholds()["water_budget"]["tight_min_pct"]
    return "short" if hi < tight else "not_short" if lo >= tight else None


def k2(items: dict, pop: list[dict]) -> dict:
    cfg = config()["k2"]
    detail, reasons = [], []
    for h in pop:
        r = items.get((h["hh_id"], "f3"), {}).get("result")
        ok, why = eligibility(r)
        if not ok:
            reasons.append(why)
            detail.append({"hh_id": h["hh_id"], "paired": False, "reason": why})
            continue
        facts = {f["fact_id"]: f for f in r["facts"]}
        b = short_state(facts.get(water_budget.coverage_fact_id(cfg["baseline_option"])))
        rec = short_state(facts.get(water_budget.coverage_fact_id(r["recommended"])))
        if b is None or rec is None:
            why = "water budget unknown or straddles the short boundary"
            reasons.append(why)
            detail.append({"hh_id": h["hh_id"], "paired": False, "reason": why})
            continue
        detail.append({"hh_id": h["hh_id"], "paired": True, "baseline": b, "recommended_option": r["recommended"],
                       "recommended": rec, "baseline_assumed": True})
    paired = [d for d in detail if d["paired"]]
    n = len(paired)
    n_base_short = sum(d["baseline"] == "short" for d in paired)
    n_rec_short = sum(d["recommended"] == "short" for d in paired)
    resolved = sum(d["baseline"] == "short" and d["recommended"] == "not_short" for d in paired)
    base_pct = round(100 * n_base_short / n, 1) if n else None
    rec_pct = round(100 * n_rec_short / n, 1) if n else None
    return kpi("K2", "Water-short plans avoided", None if not n else round(base_pct - rec_pct, 1), "percentage points",
               "SIMULATED" if n else "NOT_ESTABLISHED",
               formula_text="baseline_short_pct − recommended_short_pct over the SAME paired households; 'short' = "
                            "code water-budget coverage entirely below the tight band",
               baseline_definition=f"{cfg['baseline_option']} for every household ({cfg['baseline_basis']})",
               n_total=len(pop), n_eligible=n, n_excluded=len(pop) - n, exclusion_reasons=count_reasons(reasons),
               baseline_short_pct=base_pct, recommended_short_pct=rec_pct,
               resolved_short_pct=round(100 * resolved / n_base_short, 1) if n_base_short else None,
               resolved_short_reason=None if n_base_short else "no paired household has a short baseline",
               n_baseline_short=n_base_short, n_recommended_short=n_rec_short, n_assumed_baseline=n,
               data_sources=["F-wb-coverage facts (tools/water_budget.py): SMAP root zone, OpenLandMap soil, "
                             "data/crop_water_need.csv"],
               assumptions=[cfg["baseline_basis"], "Pump flow and irrigation weeks are labelled assumptions "
                            "(config/thresholds.yaml water_budget)"],
               limitations=["Simulated plan comparison, not measured water saved",
                            "Households with unknown or boundary-straddling water budgets are excluded (see counts)",
                            "Crop water needs for gram and safflower come from Rajasthan studies"],
               talking_point=(f"Among {n} paired households, a water-short plan drops from {base_pct}% (baseline) to "
                              f"{rec_pct}% (recommended)." if n else "Not established: no paired households."),
               per_household_detail=detail)


# ---------- K3 ----------

def first_high(plot_id: str, dates: list[str], repo) -> str | None:
    for d in dates:
        if stress_level(get_plot_stress_facts(plot_id, repo, cutoff=d))[0] == "HIGH":
            return d
    return None


def k3(repo, pop: list[dict]) -> dict:
    cfg = config()["k3"]
    ref = date.fromisoformat(cfg["reference_date"])
    hh_ids = {h["hh_id"] for h in pop}
    plots = [p for p in sorted(repo.fetch_all("plots"), key=lambda p: p["plot_id"]) if p["hh_id"] in hh_ids]
    village = {h["hh_id"]: h["village_id"] for h in pop}
    detail, leads = [], []
    for p in plots:
        dates = sorted(str(r["date"]) for r in repo.fetch_where("rain_series", village_id=village[p["hh_id"]]))
        if not dates:
            detail.append({"plot_id": p["plot_id"], "eligible": False, "reason": "no rain data"})
            continue
        hit = first_high(p["plot_id"], dates, repo)
        lead = (ref - date.fromisoformat(hit)).days if hit else None
        if lead is not None:
            leads.append(lead)
        detail.append({"plot_id": p["plot_id"], "hh_id": p["hh_id"], "eligible": True, "first_high": hit,
                       "lead_days": lead, "plot_location_synthetic": bool(p["is_synthetic"])})
    eligible = [d for d in detail if d["eligible"]]
    rain_dates = sorted(str(r["date"]) for r in repo.fetch_all("rain_series"))
    sources = sorted({r.get("source") or "unknown" for r in repo.fetch_all("rain_series")})
    return kpi("K3", "Satellite replay: days before the reference date", median(leads), "days (median)",
               "REPLAY" if eligible else "NOT_ESTABLISHED",
               formula_text="lead_days = reference_date − first date the F2 thresholds give HIGH, replayed day by day "
                            "using only observations up to that day (NDVI windows must end by the cutoff)",
               baseline_definition=f"reference date {cfg['reference_date']} "
                                   f"({'verified' if cfg.get('verified') else 'unverified candidate date: an assumption'})",
               n_total=len(plots), n_eligible=len(eligible), n_excluded=len(plots) - len(eligible),
               n_detected=len(leads), reference_date=cfg["reference_date"], reference_verified=bool(cfg.get("verified")),
               reference_source=cfg.get("source_url"), coverage_period=[rain_dates[0], rain_dates[-1]] if rain_dates else None,
               data_as_of=rain_dates[-1] if rain_dates else None,
               data_sources=[f"rain_series ({', '.join(sources)})", "ndvi_series (Sentinel-2)"],
               provenance_counts={"plots_with_synthetic_location": sum(bool(p["is_synthetic"]) for p in plots)},
               assumptions=["Reference date is an unverified candidate" if not cfg.get("verified") else "Reference date verified",
                            "Observation-date replay; satellite publication delay not modelled (CHIRPS lags by weeks)"],
               limitations=["Not an operational early-warning claim and not days before crop loss",
                            "Plot locations are synthetic points near villages; rain and NDVI values are real",
                            "Rain is village-level, so plots in the same village share rain facts",
                            "Early-season HIGH (June-July) comes from a weak monsoon onset plus low pre-sowing NDVI; leads "
                            "of 60+ days reflect early-season stress, not a forecast of the declared drought (see per-plot dates)"],
               talking_point=(f"Replaying stored {'/'.join(sources)} rain and Sentinel-2 NDVI observations "
                              f"({rain_dates[0]} to {rain_dates[-1]}), {len(leads)} of {len(eligible)} plots reached HIGH "
                              f"stress, a median {median(leads)} days before the "
                              f"{'verified' if cfg.get('verified') else 'unverified'} reference date."
                              if leads else "No plot reached HIGH stress in the replay."),
               per_household_detail=detail)


# ---------- K4 ----------

def k4(repo) -> dict:
    b = verifier_bench.latest_from_repo(repo)
    if b is None:
        return kpi("K4", "Verifier discrimination test", None, "errors caught of 20", "NOT_RUN",
                   formula_text="20 constructed errors + 10 valid controls through the production verifier",
                   limitations=["Benchmark not run yet: python scripts/run_impact.py --refresh"],
                   talking_point="Not run yet.")
    complete = b["errors_executed"] == b["errors_planned"] and b["controls_executed"] == b["controls_planned"]
    return kpi("K4", "Verifier discrimination test", b["errors_caught"], f"errors caught of {b['errors_planned']}",
               "TESTED" if complete else "NOT_RUN",
               formula_text="errors caught / constructed errors; valid controls accepted / controls. Caught = the code "
                            "verifier or the verifier agent flagged it; a crash or timeout is an error, not a catch",
               n_total=b["errors_planned"] + b["controls_planned"],
               n_eligible=b["errors_executed"] + b["controls_executed"],
               benchmark=b, controls_accepted=b["controls_accepted"], controls_planned=b["controls_planned"],
               data_sources=[f"frozen F3 fixture {b['versions'].get('fixture_request_id')}"],
               assumptions=["Expected labels set by construction (corrupted vs verbatim fact sentences), not by the verifier"],
               limitations=["One frozen F3 fixture; F4 not included in this benchmark",
                            "Must-cite rules are not exercised (cases append one driver)",
                            "Semantic checks depend on the Gemini model version recorded with the run"]
                           + ([] if complete else ["Incomplete run: see executed vs planned counts"]),
               talking_point=f"{b['errors_caught']}/{b['errors_planned']} constructed errors caught · "
                             f"{b['controls_accepted']}/{b['controls_planned']} valid controls accepted.")


# ---------- K5 ----------

def outcome_records(repo) -> list[dict]:
    hh = {h["hh_id"]: h for h in repo.fetch_all("households")}
    sr = {(r["village_id"], r["season"]): r["rain_deficit_bucket"] for r in repo.fetch_all("season_rain")}
    ck = {r["decision"]: r["crop_key"] for r in repo.fetch_all("crop_mix_keys")}
    out = []
    for o in repo.fetch_all("outcomes"):
        h = hh.get(o["hh_id"])
        if not h or not (o.get("predicted_signal") and o.get("predicted_confidence") and o.get("actual_result")):
            continue
        bucket = o.get("rain_deficit_bucket") or sr.get((h["village_id"], o["season"])) or "unknown"
        crop = o.get("crop_key") or ck.get(o.get("decision")) or "unknown"
        s, a = o["predicted_signal"], o["actual_result"]
        hit = (s == "ACT" and a == "good") or (s == "AVOID" and a != "good") or (s == "WAIT" and a == "partial")
        out.append({"context": f"{bucket}|{h.get('irrigation') or 'unknown'}|{crop}", "confidence": o["predicted_confidence"],
                    "hit": int(hit), "provenance": outcome_provenance(o)})
    return out


def confidence_gap(records: list[dict], mapping: dict, key: str) -> float | None:
    groups = {}
    for r in records:
        groups.setdefault((r["context"], r[key]), []).append(r["hit"])
    if not records:
        return None
    return round(statistics.mean(abs(mapping[r[key]] - statistics.mean(groups[(r["context"], r[key])])) for r in records), 3)


def apply_rule(records: list[dict]) -> list[dict]:
    t = settings.thresholds()["calibration"]
    high = {}
    for r in records:
        if r["confidence"] == "HIGH":
            high.setdefault(r["context"], []).append(r["hit"])
    out = []
    for r in records:
        h = high.get(r["context"], [])
        down = r["confidence"] == "HIGH" and len(h) >= t["min_n"] and statistics.mean(h) < t["high_min_hit_rate"]
        out.append({**r, "after": "MED" if down else r["confidence"]})
    return out


def k5(repo) -> dict:
    mapping = config()["k5"]["mapping"]
    recs = outcome_records(repo)
    prov = count_reasons([r["provenance"] for r in recs])
    synth = apply_rule([r for r in recs if r["provenance"] == "synthetic"])
    observed = [r for r in recs if r["provenance"] == "observed"]
    before, after = confidence_gap(synth, mapping, "confidence"), confidence_gap(synth, mapping, "after")
    status = "SIMULATED" if synth else "NOT_ESTABLISHED"
    return kpi("K5", "Simulated confidence-gap adjustment", None if before is None else round(after - before, 3),
               "change in mean |mapped confidence − bucket outcome rate|", status,
               formula_text="gap = mean over records of |mapping[confidence] − outcome rate of that record's (context, "
                            "confidence) bucket|; before uses the predicted label, after applies the F7 rule (HIGH→MED when "
                            "past HIGH calls in the context were right < 70% with n ≥ 5); same records, equal weights",
               baseline_definition="the same synthetic records before the F7 rule",
               n_total=len(recs), n_eligible=len(synth), n_excluded=len(recs) - len(synth),
               exclusion_reasons={k: v for k, v in prov.items() if k != "synthetic"},
               provenance_counts=prov, gap_before=before, gap_after=after,
               n_downgraded=sum(r["after"] != r["confidence"] for r in synth),
               observed_gap=confidence_gap(observed, mapping, "confidence") if observed else None,
               observed_note=None if observed else "no observed outcomes: real-world calibration not established",
               correctness_definition="hit = ACT→good, AVOID→partial/loss, WAIT→partial (calibration_v)",
               assumptions=[f"Illustrative mapping {mapping}: not empirically established probabilities"],
               limitations=["In-sample demonstration: the same records drive the adjustment and the evaluation",
                            "Synthetic outcomes only; synthetic and observed are never pooled",
                            "Context = rain bucket × irrigation × crop key, as in calibration_v"],
               talking_point=(f"On {len(synth)} synthetic records the F7 rule changes the confidence gap from {before} to "
                              f"{after} (in-sample, illustrative mapping)." if synth else "Not established."))


# ---------- K6 ----------

def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))]


def k6(repo, pop: list[dict]) -> dict:
    n = len(pop)
    tenant = sum(h.get("tenure") in ("tenant", "sharecrop") for h in pop)
    woman = sum(h.get("primary_cultivator_gender") == "woman" for h in pop)
    union = sum(h.get("tenure") in ("tenant", "sharecrop") or h.get("primary_cultivator_gender") == "woman" for h in pop)
    logs = repo.fetch_all("decision_log")
    fresh = [r for r in logs if r["feature"] in ("f3", "f4") and r.get("orchestrator_used") in ("adk", "plain")]
    replay = [r for r in logs if r["feature"] in ("f3", "f4") and r.get("orchestrator_used") in ("cache", "rule")]
    calls = []
    for r in fresh:
        out = r["output_json"] if isinstance(r["output_json"], dict) else json.loads(r["output_json"] or "{}")
        usage = next((t for t in reversed(out.get("trace", [])) if t.get("step") == "usage"), None)
        if usage and "calls" in usage.get("detail", {}):
            calls.append(sum(1 for c in usage["detail"]["calls"] if not c.get("cached")))
    f1 = []
    for r in logs:
        if r["feature"] != "f1":
            continue
        out = r["output_json"] if isinstance(r["output_json"], dict) else json.loads(r["output_json"] or "{}")
        if out.get("type") == "final":
            f1.append(out.get("questions_asked", 0))
    lat = [r["latency_ms"] for r in fresh if r.get("latency_ms") is not None]
    withheld = 0
    for r in fresh:
        out = r["output_json"] if isinstance(r["output_json"], dict) else json.loads(r["output_json"] or "{}")
        withheld += bool(out.get("withheld"))
    return kpi("K6", "Demo coverage and runtime", round(100 * union / n, 1) if n else None,
               "% tenant/sharecropper or woman cultivator (demo)", "MEASURED",
               formula_text="coverage = share of the demo population; runtime from decision_log telemetry",
               n_total=n, n_eligible=n,
               coverage={"tenant_or_sharecrop": tenant, "woman_primary_cultivator": woman, "union": union, "n": n,
                         "tenure_unknown": sum(h.get("tenure") is None for h in pop),
                         "gender_unknown": sum(h.get("primary_cultivator_gender") is None for h in pop),
                         "population": "synthetic demo households"},
               runtime={"fresh_decisions": len(fresh), "replayed_or_fallback": len(replay),
                        "latency_p50_ms": pct(lat, 50), "latency_p90_ms": pct(lat, 90),
                        "model_calls_mean": round(statistics.mean(calls), 2) if calls else None,
                        "model_calls_measured": len(calls),
                        "completed_pass": sum(r.get("verifier_status") == "PASS" for r in fresh),
                        "failed": sum(r.get("verifier_status") == "FAIL" for r in fresh), "withheld": withheld,
                        "f1_completed": len(f1), "f1_questions_mean": round(statistics.mean(f1), 2) if f1 else None,
                        "cost": "₹ cost not measured"},
               limitations=["Demo composition, not real-world reach", "Runtime covers all decisions logged locally",
                            "Model calls exclude cache hits; this is not a monetary cost"],
               talking_point=f"{union} of {n} demo households are tenant/sharecropper or woman-cultivated.")


# ---------- K7 ----------

def k7() -> dict:
    return kpi("K7", "Evidence preparation target", config()["k7"]["target_days"], "days (target)", "TARGET",
               formula_text="target only; no defined, validated claim-readiness process has been timed",
               file_generation_ms=None, file_generation_note="F6 evidence pack is not built (SCREEN only)",
               limitations=["Not measured", "File generation would not prove claim readiness, eligibility or submission",
                            "No reporting-deadline rule is asserted"],
               talking_point="Target: an evidence file within 3 days of a loss (not measured; F6 is roadmap).")


# ---------- run ----------

def honesty_banner(pop, items, kpis, repo) -> str:
    rain = sorted(str(r["date"]) for r in repo.fetch_all("rain_series"))
    missing = sum(1 for v in items.values() if v["result"] is None)
    excluded = sum((k.get("n_excluded") or 0) for k in kpis if k["id"] in ("K1", "K2"))
    synth = sum(bool(h.get("is_synthetic")) for h in pop)
    parts = [f"Population: {len(pop)} households ({synth} synthetic)."]
    if rain:
        parts.append(f"Rain data {rain[0]} to {rain[-1]} ({', '.join(sorted({r.get('source') or '?' for r in repo.fetch_all('rain_series')}))}).")
    parts.append("Normal animal values, outcomes and plot locations are synthetic; KPIs K1, K2, K5 are simulations.")
    parts.append(f"K3 reference date {config()['k3']['reference_date']} is "
                 f"{'verified' if config()['k3'].get('verified') else 'an unverified candidate'}; publication delay not modelled.")
    parts.append(f"Cached assessments missing: {missing}; households excluded from K1/K2: {excluded}.")
    return " ".join(parts)


class CacheIncomplete(Exception):
    """API replay found missing cache entries for this backend's data snapshot; nothing was saved."""


def run(repo, households: str = "demo", engine: str | None = None, do_refresh: bool = False, log=print,
        require_complete: bool = False) -> dict:
    t0 = time.perf_counter()
    engine = engine or settings.orchestrator()
    reader = repo if do_refresh else ReadCache(repo)
    pop = population(reader, households)
    versions = version_hash()
    stats = {"built": 0, "model_calls": 0}
    bench_calls = 0
    if do_refresh:
        stats = refresh(repo, pop, engine, versions, log)
        b = verifier_bench.run_benchmark(repo)
        bench_calls = b["model_calls"]
        log(f"  benchmark {b['benchmark_run_id']}: {b['errors_caught']}/{b['errors_planned']} caught, "
            f"{b['controls_accepted']}/{b['controls_planned']} controls accepted ({bench_calls} model calls)")
        reader = ReadCache(repo)
    items = lookup(reader, pop, engine, versions)
    missing = sum(v["result"] is None for v in items.values())
    if require_complete and missing:
        raise CacheIncomplete(f"{missing} of {len(items)} cached assessments are missing for this data snapshot; "
                              "the last saved run is kept. Rebuild with: python scripts/run_impact.py --refresh")
    kpis = [k1(items, pop), k2(items, pop), k3(reader, pop), k4(reader), k5(reader), k6(reader, pop), k7()]
    used = sorted({v["result"]["orchestrator_used"] for v in items.values() if v["result"]})
    bench = next(k for k in kpis if k["id"] == "K4").get("benchmark") or {}
    run_row = {
        "run_id": f"IR-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
        "created_at": datetime.now(timezone.utc).isoformat(), "population_snapshot_id": population_snapshot_id(pop),
        "n_households": len(pop), "kpis_json": json.dumps(kpis, ensure_ascii=False, default=str),
        "assumptions_json": json.dumps(config(), ensure_ascii=False, default=str), "engine_requested": engine,
        "engines_used": ",".join(used), "data_backend": os.getenv("DATA_BACKEND", "postgres"),
        "cache_snapshot_id": sha(sorted(v["key"] for v in items.values() if v["result"])),
        "benchmark_run_id": bench.get("benchmark_run_id"),
        "version_metadata": json.dumps({"version_hash": versions, "model": llm.gemini_model(), "mode": "refresh" if do_refresh
                                        else "replay", "cache_missing": sum(v["result"] is None for v in items.values()),
                                        "cache_total": len(items), "model_calls": stats["model_calls"] + bench_calls,
                                        "runtime_s": round(time.perf_counter() - t0, 1),
                                        "honesty_banner": honesty_banner(pop, items, kpis, reader)})}
    repo.insert("impact_runs", [run_row])
    return to_api(run_row)


def to_api(row: dict) -> dict:
    meta = json.loads(row["version_metadata"] or "{}")
    return {"run_id": row["run_id"], "created_at": str(row["created_at"]), "n_households": row["n_households"],
            "population_snapshot_id": row["population_snapshot_id"], "cache_snapshot_id": row["cache_snapshot_id"],
            "benchmark_run_id": row["benchmark_run_id"], "engine_requested": row["engine_requested"],
            "engines_used": row["engines_used"], "data_backend": row["data_backend"], "meta": meta,
            "honesty_banner": meta.get("honesty_banner"), "assumptions": json.loads(row["assumptions_json"] or "{}"),
            "kpis": json.loads(row["kpis_json"] or "[]")}


def latest(repo) -> dict | None:
    rows = repo.fetch_all("impact_runs")
    return to_api(max(rows, key=lambda r: str(r["created_at"]))) if rows else None


def analytical(api: dict) -> list[dict]:
    """KPI content without run ids, timestamps or replay telemetry (K6 runtime): identical snapshots -> identical."""
    return [{k: v for k, v in kp.items() if k != "runtime"} for kp in api["kpis"]]
