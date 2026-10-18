"""Run F3 for a household and print a readable Agent Trace. Calls Gemini unless answers are cached.

Usage: python scripts/run_f3.py HH_ID [--engine adk|plain] [--inject-bad-fact] [--force-adk-fail]
                                      [--runs N] [--no-cache]
  --inject-bad-fact  corrupt the first assessor answer (fake fact_id); the verifier must catch it
  --force-adk-fail   make the ADK engine raise, to show the fallback to the plain engine
  --runs N           repeat N times and report whether the signals matched every time
  --no-cache         bypass the LLM disk cache (real Gemini calls; use with --runs for repeatability)
"""
import argparse
import asyncio
import os

from common import require_household
from core import llm
from core.schemas import FeatureResult
from orchestrators import selector
from orchestrators.base import Engine, EngineOutput, EngineTask


class ForcedFailure(Engine):
    name = "adk"

    async def run(self, task: EngineTask) -> EngineOutput:
        raise RuntimeError("forced ADK failure (--force-adk-fail)")

    async def run_agents(self, feature, specs, state):
        raise RuntimeError("forced ADK failure (--force-adk-fail)")


def show(r: FeatureResult) -> None:
    for t in r.trace:
        d = t.detail
        ms = f"  [{t.ms} ms]" if t.ms is not None else ""
        if t.step == "engine":
            print(f"\n{t.title}")
        elif t.step == "planner" and "tools" in d:
            print(f"🧭 Planner chose: {', '.join(d['tools']) or '(none)'}{ms}")
            for line in (d.get("reasoning") or "").splitlines():
                if line.strip():
                    print(f"     {line.strip()}")
        elif t.step == "planner":
            print(f"🧭 Planner missed required tools; code called: {', '.join(d['missing'])}")
        elif t.step == "tool":
            print(f"   🛰️ {d['tool']}({', '.join(map(str, d['args']))}) -> {len(d['fact_ids'])} facts{ms}")
        elif t.step == "facts":
            print(f"🛰️ Facts ({len(d['facts'])}):")
            for f in d["facts"]:
                print(f"     {f['fact_id']:34s} {f['text'][:90]}{'  [SYNTHETIC]' if f['is_synthetic'] else ''}")
        elif t.step == "inject":
            print(f"💉 Injected error: {d['change']}")
        elif t.step == "assessor":
            print(f"{t.title}{ms}: " + "; ".join(f"{o['name']} {o['signal']}/{o['confidence']}" for o in d["options"])
                  + f"  (assessor picked: {d['recommended']})")
        elif t.step == "verifier_code":
            print(f"{t.title} (attempt {d['attempt']})")
            for i in d["issues"]:
                print(f"     - {i}")
        elif t.step == "verifier_agent":
            print(f"🔎 {t.title}{ms}")
            for i in d.get("issues") or []:
                print(f"     - {i}")
        elif t.step == "calibration":
            print(f"{t.title if t.title.startswith('📊') else '📊 ' + t.title}: {d.get('text', '')}")
            for c in d.get("changes") or []:
                print(f"     - {c}")
        elif t.step == "safety":
            print(f"🛡️ {t.title}")
            for n in d["notes"]:
                print(f"     - {n}")
        elif t.step == "evidence":
            print(f"📚 {t.title}")
        elif t.step == "usage":
            print(f"💰 {t.title}")
            for c in d.get("calls", []):
                print(f"     {c['engine']}/{c['role']:9s} backend={c['backend']} model={c['model']} "
                      f"latency_ms={c['latency_ms']} tokens_in={c['tokens_in']} tokens_out={c['tokens_out']}"
                      f"{' (cached)' if c['cached'] else ''}")

    print("\nFinal:")
    print(f"  {'option':20s} {'signal':6s} {'conf':5s} {'water':6s} {'cash':5s} risk")
    for o in r.options:
        mark = "  <- recommended" if o.name == r.recommended else ""
        print(f"  {o.name:20s} {o.signal:6s} {o.confidence:5s} {o.water_fit:6s} {o.cash_at_risk:5s} "
              f"{o.failure_risk if o.failure_risk is not None else 'n/a'}{mark}")
    for n in r.notes:
        print(f"  note: {n}")
    e = r.explain
    if e.why_recommended:
        print("\nWhy recommended:")
        for d in e.why_recommended:
            print(f"  - {d.text}  [{d.fact_id}]")
        print("Why not the others:")
        for w in e.why_not_others:
            print(f"  - {w.option}: {w.text}  [{w.fact_id}]")
    if e.what_would_change:
        print(f"What would change it: {e.what_would_change['text']}")
    print(f"Confidence reason: {'; '.join(e.confidence_reason) if e.confidence_reason else '-'}")
    print(f"Data gaps: {'; '.join(e.data_gaps) or 'none'}")
    if e.plain_explanation_mr:
        print(f"मराठी: {e.plain_explanation_mr}")
        print(f"हिंदी: {e.plain_explanation_hi}")
    print(f"  verifier_status={r.verifier_status}  safety_rule_applied={r.safety_rule_applied}  "
          f"engine={r.orchestrator_used}  backend={r.genai_backend}  model={r.gemini_model}  "
          f"fallback_reason={r.fallback_reason}  latency={r.latency_ms} ms")


def signature(r: FeatureResult) -> tuple:
    return r.recommended, tuple(sorted((o.name, o.signal) for o in r.options))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hh_id")
    ap.add_argument("--engine", choices=["adk", "plain"])
    ap.add_argument("--inject-bad-fact", action="store_true")
    ap.add_argument("--force-adk-fail", action="store_true")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    require_household(args.hh_id)
    from repo.base import get_repo
    get_repo().init_schema()  # idempotent; adds new decision_log columns if missing
    try:
        llm.verify_backend(check_adk=args.engine != "plain")
    except RuntimeError as exc:
        raise SystemExit(f"Backend check failed: {exc}") from None
    if args.no_cache:
        os.environ["LLM_CACHE"] = "off"
    engines = None
    if args.force_adk_fail:
        if args.engine == "plain":
            ap.error("--force-adk-fail needs the ADK engine first (omit --engine or use --engine adk)")
        os.environ["ENGINE_FALLBACK"] = "on"
        engines = {"adk": ForcedFailure()}
        args.engine = "adk"

    results = []
    for i in range(args.runs):
        print(f"\n===== Run {i + 1}/{args.runs}: F3 for {args.hh_id} =====")
        r = asyncio.run(selector.run("f3", args.hh_id, inject="bad_fact_id" if args.inject_bad_fact else None,
                                     engine=args.engine, engines=engines))
        show(r)
        results.append(r)
    if args.runs > 1:
        sigs = [signature(r) for r in results]
        same = sigs.count(sigs[0])
        shown = [(r.recommended, next(o.signal for o in r.options if o.name == r.recommended)) for r in results]
        rec_same = shown.count(shown[0])
        print(f"\nRecommended option + signal matched in {rec_same}/{args.runs} runs: {'YES' if rec_same == args.runs else 'NO'}")
        print(f"All option signals matched in {same}/{args.runs} runs: {'YES' if same == args.runs else 'NO'}")
        for i, s in enumerate(sigs, 1):
            print(f"  run {i}: recommended {s[0]}; " + ", ".join(f"{n} {sig}" for n, sig in s[1]))
        if os.getenv("LLM_CACHE", "disk") != "off":
            print("  (cache on: repeats may be cache hits; add --no-cache for real repeat calls)")


if __name__ == "__main__":
    main()
