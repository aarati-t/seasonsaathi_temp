"""P5b impact simulator CLI (SPEC section 8 KPIs). All KPI maths in code (features/impact.py).

Usage:
  python scripts/run_impact.py                    # cached replay of the demo population: no model calls, no outcomes
  python scripts/run_impact.py --refresh          # build missing cached assessments + run the K4 benchmark (model calls)
  python scripts/run_impact.py --households all   # LOCAL ONLY: include real (non-synthetic) households
  python scripts/run_impact.py --engine plain --export impact.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from features import impact  # noqa: E402
from repo.base import get_repo  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--households", choices=["demo", "all"], default="demo")
    ap.add_argument("--engine", choices=["adk", "plain"], default=None)
    ap.add_argument("--refresh", action="store_true", help="build missing cache entries and run the K4 benchmark")
    ap.add_argument("--export", type=Path, help="write the run (all KPI fields) as JSON")
    args = ap.parse_args()

    t0 = time.perf_counter()
    repo = get_repo()
    repo.init_schema()
    out = impact.run(repo, households=args.households, engine=args.engine, do_refresh=args.refresh)
    m = out["meta"]
    print(f"\nrun {out['run_id']}  mode={m['mode']}  households={out['n_households']}  "
          f"cache {m['cache_total'] - m['cache_missing']}/{m['cache_total']}  model calls={m['model_calls']}  "
          f"runtime={round(time.perf_counter() - t0, 1)} s")
    print(f"{'KPI':4} {'value':>12}  {'status':16} {'eligible/total':15} baseline | limitations")
    for k in out["kpis"]:
        v = "null" if k["value"] is None else k["value"]
        el = f"{k['n_eligible'] if k['n_eligible'] is not None else '-'}/{k['n_total'] if k['n_total'] is not None else '-'}"
        print(f"{k['id']:4} {str(v):>12}  {k['status']:16} {el:15} {(k['baseline_definition'] or '-')[:50]} | "
              f"{'; '.join(k['limitations'][:1])[:70]}")
        if k["exclusion_reasons"]:
            print(f"{'':4} excluded: {k['exclusion_reasons']}")
    print("\n" + (out["honesty_banner"] or ""))
    if args.export:
        args.export.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        print(f"wrote {args.export}")


if __name__ == "__main__":
    main()
