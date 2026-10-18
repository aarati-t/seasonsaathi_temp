"""Print every fact the tools return for a household, plus its top-3 similar households. No LLM calls.

Usage: python scripts/show_facts.py HH_ID
"""
import argparse
import textwrap

from common import require_household
from features.f3_cropmix import CROP_MIXES, build_context
from orchestrators.base import FactStore, make_tools
from tools.household import household_facts
from tools.peers import similar

COLS = [("fact_id", 32), ("name", 58), ("value", 12), ("unit", 16), ("data_source", 11), ("source", 30),
        ("synthetic", 9)]


def cell(v, width: int) -> str:
    s = "" if v is None else str(v)
    return (s[:width - 1] + "…") if len(s) > width else s.ljust(width)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hh_id")
    args = ap.parse_args()
    require_household(args.hh_id)

    ctx = build_context(args.hh_id)
    store = FactStore()
    store.add(household_facts(ctx.hh, ctx.village))
    tools = {fn.__name__: fn for fn in make_tools(store, ctx.repo)}
    tools["get_rain_status"](ctx.hh["village_id"])
    tools["find_similar_households"](ctx.hh_id)
    for c in dict.fromkeys(ctx.option_contexts.values()):
        tools["get_calibration"](c)
    for commodity in sorted({c for crops in CROP_MIXES.values() for c in crops}):
        tools["get_prices"](commodity, ctx.district)
    for plot in ctx.repo.fetch_where("plots", hh_id=ctx.hh_id):
        tools["get_crop_health"](plot["plot_id"])

    print(f"Household {ctx.hh_id}: {ctx.hh['irrigation']}, cash {ctx.hh['cash_buffer_band']}, "
          f"village {ctx.village['name']} ({ctx.district}); calibration contexts: {', '.join(dict.fromkeys(ctx.option_contexts.values()))}\n")
    print(" ".join(cell(c, w) for c, w in COLS))
    print(" ".join("-" * w for _, w in COLS))
    for f in store.as_list():
        row = {"fact_id": f["fact_id"], "name": f["text"], "value": f.get("value"), "unit": f.get("unit"),
               "data_source": f.get("data_source"), "source": f.get("source"), "synthetic": "SYNTHETIC" if f.get("is_synthetic") else ""}
        print(" ".join(cell(row[c], w) for c, w in COLS))
    print(f"\n{len(store.facts)} facts from {len(store.calls)} tool calls\n")

    peers = similar(ctx.hh_id, ctx.repo, 3)
    print("Top-3 similar households (cosine on profile embeddings, households with past outcomes only):")
    if peers is None:
        print(f"  none: {ctx.hh_id} has no embedding (run scripts/embed_profiles.py)")
        return
    households = {h["hh_id"]: h for h in ctx.repo.fetch_all("households")}
    for score, pid in peers:
        h = households[pid]
        print(f"  {pid:10s} cosine {score:.4f}  {h['irrigation']}, cash {h['cash_buffer_band']}, "
              f"{h['acres']} acres, {h['tenure']}")
        print(textwrap.indent(textwrap.fill(h["profile_text_norm"], 100), "             "))


if __name__ == "__main__":
    main()
