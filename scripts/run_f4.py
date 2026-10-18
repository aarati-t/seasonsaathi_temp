"""Run F4 (livestock and fodder) for a household and print a readable trace. Calls Gemini unless cached.

Usage: python scripts/run_f4.py HH_ID [--engine adk|plain] [--inject-bad-fact] [--no-cache]
"""
import argparse
import asyncio
import os

from common import require_household
from core import llm
from core.schemas import F4Result
from orchestrators import selector
from repo.base import get_repo


def rs(m) -> str:
    if m is None or m.value is None and m.low is None:
        return "-"
    if m.low is not None and m.high is not None and m.low != m.high:
        return f"₹{m.low:,}-{m.high:,}"
    return f"₹{(m.value if m.value is not None else m.low):,}"


def show(r: F4Result) -> None:
    if not r.applicable:
        print(f"\nF4 not applicable for {r.hh_id}: {r.reason}  (no Gemini call)")
        return
    for t in r.trace:
        d = t.detail
        ms = f"  [{t.ms} ms]" if t.ms is not None else ""
        if t.step == "engine":
            print(f"\n{t.title}")
        elif t.step == "planner" and "tools" in d:
            print(f"🧭 Planner chose: {', '.join(d['tools'])}{ms}")
        elif t.step == "planner":
            print(f"🧭 Required tools called by code: {', '.join(d['missing'])}")
        elif t.step == "facts":
            print(f"🛰️ Facts ({len(d['facts'])}):")
            for f in d["facts"]:
                if f["fact_id"].startswith(("F-f4-", "F-rain-deficit", "F-peer-outcome", "F-hh-cash", "F-hh-off")):
                    print(f"     {f['fact_id']:36s} {f['text'][:110]}{'  [SYNTHETIC]' if f['is_synthetic'] else ''}")
        elif t.step == "inject":
            print(f"💉 Injected error: {d['change']}")
        elif t.step == "assessor":
            print(f"{t.title}{ms}: " + "; ".join(f"{o['name']} risk {o.get('risk')}/{o['confidence']}" for o in d["options"]))
        elif t.step == "verifier_code":
            print(f"{t.title} (attempt {d['attempt']})")
            for i in d["issues"]:
                print(f"     - {i}")
        elif t.step == "verifier_agent":
            print(f"🔎 {t.title}{ms}")
        elif t.step == "calibration":
            for c in d.get("changes", []):
                print(f"📊 {c}")
        elif t.step == "safety":
            print(f"🛡️ {t.title}  risks={d['risks']}")
        elif t.step in ("evidence", "usage"):
            print(f"{'📚' if t.step == 'evidence' else '💰'} {t.title}")

    print("\nFinal:")
    print(f"  {'option':22s} {'risk':>4s} {'conf':5s} {'cash press.':11s} {'cash needed':22s} {'value kept':12s} {'sale now':18s} support")
    for o in r.options:
        mark = "  <- recommended" if o.name == r.recommended else ""
        print(f"  {o.name:22s} {o.risk:>4d} {o.confidence:5s} {o.cash_pressure:11s} {rs(o.cash_needed):22s} "
              f"{rs(o.asset_value_kept):12s} {rs(o.sale_proceeds):18s} {','.join(o.support_ids) or '-'}{mark}")
    e = r.explain
    print("\nWhy recommended:")
    for d in e.why_recommended:
        print(f"  - {d.text}  [{d.fact_id}]")
    print("Why not the others:")
    for w in e.why_not_others:
        print(f"  - {w.option}: {w.text}  [{w.fact_id}]")
    if e.what_would_change:
        print(f"What would change it: {e.what_would_change['text']}")
    print(f"Confidence reason: {'; '.join(e.confidence_reason)}")
    print(f"Data gaps: {'; '.join(e.data_gaps) or 'none'}")
    print(f"मराठी: {e.plain_explanation_mr}")
    print(f"हिंदी: {e.plain_explanation_hi}")
    for n in r.notes:
        print(f"  note: {n}")
    print(f"  verifier_status={r.verifier_status}  engine={r.orchestrator_used}  backend={r.genai_backend}  "
          f"model={r.gemini_model}  fallback_reason={r.fallback_reason}  latency={r.latency_ms} ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hh_id")
    ap.add_argument("--engine", choices=["adk", "plain"])
    ap.add_argument("--inject-bad-fact", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    require_household(args.hh_id)
    get_repo().init_schema()
    if args.no_cache:
        os.environ["LLM_CACHE"] = "off"
    try:
        llm.verify_backend(check_adk=args.engine != "plain")
    except RuntimeError as exc:
        raise SystemExit(f"Backend check failed: {exc}") from None
    r = asyncio.run(selector.run("f4", args.hh_id, inject="bad_fact_id" if args.inject_bad_fact else None,
                                 engine=args.engine))
    show(r)


if __name__ == "__main__":
    main()
