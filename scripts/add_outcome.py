"""Record real outcomes for a household's latest F3 decision (learning-loop demo).

Usage: python scripts/add_outcome.py HH_ID --result loss|partial|good [--n 3] [--season rabi-2026-27]
                                     [--request-id ID] [--note TEXT]
Rows are inserted with is_synthetic=false (they count as REAL in calibration_v). Use only with
outcomes you actually observed, or for a clearly labelled demo; --n repeats the same outcome n times.
"""
import argparse

from common import require_household
from features import learning
from repo.base import get_repo
from tools.calibration import get_calibration


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hh_id")
    ap.add_argument("--result", required=True, choices=["loss", "partial", "good"])
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--season", default="rabi-2026-27")
    ap.add_argument("--request-id")
    ap.add_argument("--note", default="recorded with scripts/add_outcome.py")
    args = ap.parse_args()
    require_household(args.hh_id)
    repo = get_repo()
    repo.init_schema()
    request_id = args.request_id
    if not request_id:
        rows = [r for r in repo.fetch_where("decision_log", hh_id=args.hh_id, feature="f3") if r["verifier_status"] == "PASS"]
        if not rows:
            raise SystemExit(f"No F3 decision for {args.hh_id} yet: run python scripts/run_f3.py {args.hh_id} first")
        request_id = max(rows, key=lambda r: r["request_id"])["request_id"]
    saved = [learning.record_outcome(repo, args.hh_id, request_id, args.season, args.result, note=args.note)
             for _ in range(args.n)]
    o = saved[0]
    print(f"Recorded {len(saved)} REAL outcome(s) for {args.hh_id} (request {request_id}): "
          f"{o['decision']} predicted {o['predicted_signal']}/{o['predicted_confidence']} -> actual {args.result}")
    if o["rain_deficit_bucket"] and o["crop_key"]:
        from tools.household import load_household
        hh, _ = load_household(args.hh_id, repo)
        ctx = f"{o['rain_deficit_bucket']}|{hh['irrigation']}|{o['crop_key']}"
        print(get_calibration(ctx, repo)[0]["text"])


if __name__ == "__main__":
    main()
