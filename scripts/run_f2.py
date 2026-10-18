"""Run F2 (season risk watch) for one plot, or scan every plot. Calls Gemini only for MED/HIGH, non-duplicate plots.

Usage: python scripts/run_f2.py PLOT_ID [--engine adk|plain] [--inject-bad-number]
       python scripts/run_f2.py --scan [--engine adk|plain]
"""
import argparse
import asyncio

from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from core import llm
from features import f2_watch
from orchestrators import selector
from repo.base import get_repo


def show(r) -> None:
    print(f"\nPlot {r.plot_id} (household {r.hh_id}): stress {r.stress_level}; crossed: {r.crossed or 'none'}")
    for f in r.facts:
        print(f"   {f.fact_id:20s} {f.text}  [{f.source}]{' [SYNTHETIC]' if f.is_synthetic else ''}")
    for t in r.trace:
        d = t.detail
        if t.step in ("planner", "writer"):
            extra = f": {d.get('next_step')} - {d.get('reason')}" if t.step == "planner" and "next_step" in d else ""
            print(f"   {t.title} ({t.backend}/{t.model}){extra}")
        elif t.step in ("dedupe", "publish", "inject"):
            print(f"   {t.title}")
        elif t.step == "verifier_code":
            print(f"   {t.title} (attempt {d['attempt']})" + "".join(f"\n      - {i}" for i in d["issues"]))
        elif t.step == "usage":
            print(f"   💰 {t.title}")
    if r.alert and not r.deduped:
        print(f"\n   next_step: {r.next_step}\n   मराठी: {r.alert['text_mr']}\n   हिंदी: {r.alert['text_hi']}\n"
              f"   fact_ids: {r.alert['fact_ids']}")
    for n in r.notes:
        print(f"   note: {n}")
    print(f"   verifier_status={r.verifier_status} engine={r.orchestrator_used} deduped={r.deduped} latency={r.latency_ms} ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plot_id", nargs="?")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--engine", choices=["adk", "plain"])
    ap.add_argument("--inject-bad-number", action="store_true")
    args = ap.parse_args()
    if not args.scan and not args.plot_id:
        ap.error("give a PLOT_ID or --scan")
    repo = get_repo()
    repo.init_schema()
    try:
        llm.verify_backend(check_adk=args.engine != "plain")
    except RuntimeError as exc:
        raise SystemExit(f"Backend check failed: {exc}") from None
    if args.scan:
        out = asyncio.run(f2_watch.scan(repo, engine=args.engine))
        s = out["summary"]
        print(f"Scanned {s['plots']} plots: HIGH {s['HIGH']}, MED {s['MED']}, LOW {s['LOW']}; "
              f"alerts created {s['alerts_created']}, deduped {s['deduped']}; skipped {len(s['skipped'])}")
        for k in s["skipped"]:
            print(f"   skipped {k['hh_id']}: {k['reason']}")
        return
    try:
        ctx = f2_watch.build_context(args.plot_id, repo)
    except KeyError as exc:
        raise SystemExit(f"{exc.args[0]} (plot ids look like PLOT-SYN-001 or PLOT-HH-DEMO-FLOOR)") from None
    r = asyncio.run(selector.execute(f2_watch, ctx, "f2", ctx.hh["hh_id"], engine=args.engine,
                                     inject="bad_number" if args.inject_bad_number else None, use_result_cache=False))
    show(r)


if __name__ == "__main__":
    main()
