"""Print the F7 calibration table (calibration_v) and where the downgrade rule would fire. No LLM calls.

Usage: python scripts/show_calibration.py [--context ">30|rainfed|gram"] [--min-n 1]
"""
import argparse

from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from core.settings import thresholds
from repo.base import get_repo


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--context", help="one context_bucket, e.g. '>30|rainfed|gram'")
    ap.add_argument("--min-n", type=int, default=1, help="hide rows with fewer past calls")
    args = ap.parse_args()
    t = thresholds()["calibration"]
    repo = get_repo()
    repo.init_schema()
    rows = repo.fetch_where("calibration_v", context_bucket=args.context) if args.context else repo.fetch_all("calibration_v")
    rows = [r for r in rows if r["n"] >= args.min_n]
    print(f"Rule: HIGH and hit_rate < {t['high_min_hit_rate']} and n >= {t['min_n']} -> MED; n < {t['min_n']} -> no downgrade\n")
    print(f"{'context_bucket':34s} {'conf':5s} {'hit_rate':>8s} {'n':>4s} {'real':>5s} {'synth':>6s}  effect on a HIGH call")
    for r in sorted(rows, key=lambda r: (r["context_bucket"], ["HIGH", "MED", "LOW"].index(r["confidence_level"]))):
        hr = float(r["hit_rate"])
        effect = ""
        if r["confidence_level"] == "HIGH":
            effect = ("insufficient history" if r["n"] < t["min_n"] else
                      "DOWNGRADE to MED" if hr < t["high_min_hit_rate"] else "keep HIGH")
        print(f"{r['context_bucket']:34s} {r['confidence_level']:5s} {hr:8.2f} {r['n']:4d} {r['n_real']:5d} {r['n_synthetic']:6d}  {effect}")
    if not rows:
        print("(no rows)")


if __name__ == "__main__":
    main()
