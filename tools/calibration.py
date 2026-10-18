"""Tool: get_calibration(context). Past hit rate per confidence level for one context (SPEC F7). Facts only.

context = "<rain_deficit_bucket>|<irrigation>|<crop_key>", e.g. ">30|rainfed|gram".
rain_deficit_bucket: 0-15 / 15-30 / >30 % (config/thresholds.yaml calibration.rain_buckets).
Numbers come from the calibration_v view (Postgres and BigQuery use the same SQL), which counts real
(is_synthetic=false) outcomes automatically. The downgrade rule itself runs in code after the assessor
(apply_calibration_rule), not in Gemini.
"""
import re

from core.schemas import AssessorOutput
from core.settings import thresholds
from repo.base import Repo, get_repo

SOURCE = "calibration_v (outcomes table)"
LEVELS = ["HIGH", "MED", "LOW"]


def rain_bucket(deficit_pct: float | None) -> str:
    """0-15 / 15-30 / >30 (% below normal). A surplus (negative deficit) is in 0-15."""
    if deficit_pct is None:
        return "unknown"
    low, high = thresholds()["calibration"]["rain_buckets"]
    return "0-15" if deficit_pct < low else "15-30" if deficit_pct <= high else ">30"


def outcome_provenance(row: dict) -> str:
    """observed | synthetic | demo | legacy_unverified. Older rows without the column are never treated as observed."""
    if row.get("provenance"):
        return row["provenance"]
    if row.get("is_synthetic"):
        return "synthetic"
    return "demo" if str(row.get("season", "")).startswith("learning-demo") else "legacy_unverified"


def context(bucket: str, irrigation: str | None, crop_key: str) -> str:
    return f"{bucket}|{irrigation or 'unknown'}|{crop_key}"


def fact_id_for(ctx: str) -> str:
    slug = ctx.replace(">", "gt").replace("|", "-").replace("+", "-")
    return "F-cal-" + re.sub(r"[^A-Za-z0-9-]", "", slug)


def get_calibration(context: str, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    rows = {r["confidence_level"]: r for r in repo.fetch_where("calibration_v", context_bucket=context)}
    levels = {}
    for lvl in LEVELS:
        r = rows.get(lvl)
        if r:
            levels[lvl] = {"hit_rate": round(float(r["hit_rate"]), 2), "n": int(r["n"]), "n_real": int(r["n_real"]),
                           "n_synthetic": int(r["n_synthetic"]), "n_demo": int(r.get("n_demo") or 0),
                           "n_unverified": int(r.get("n_unverified") or 0)}
        else:
            levels[lvl] = {"hit_rate": None, "n": 0, "n_real": 0, "n_synthetic": 0, "n_demo": 0, "n_unverified": 0}
    # n_real = observed outcomes only (provenance); synthetic/demo rows never count as real (P5b D/E).
    parts = [f"{lvl} {round(v['hit_rate'] * 100)}% right (n={v['n']}, {v['n_real']} observed)" if v["n"]
             else f"{lvl} no past calls" for lvl, v in levels.items()]
    synthetic = any(v["n_synthetic"] or v["n_demo"] or v["n_unverified"] for v in levels.values())
    high = levels["HIGH"]
    return [{"fact_id": fact_id_for(context), "name": "calibration", "context": context, "levels": levels,
             "value": round(high["hit_rate"] * 100) if high["hit_rate"] is not None else None, "unit": "% HIGH calls right",
             "text": f"Calibration for {context}: " + "; ".join(parts), "source": SOURCE, "source_url": None,
             "is_synthetic": synthetic}]


def apply_calibration_rule(output: AssessorOutput, option_contexts: dict[str, str], facts: dict[str, dict]) -> tuple[list[str], list[dict]]:
    """F7 rule in CODE: HIGH and hit_rate < high_min_hit_rate and n >= min_n -> MED.
    n < min_n -> no downgrade, note 'insufficient history (n=X)'. Returns (notes, per-option details)."""
    t = thresholds()["calibration"]
    notes, details = [], []
    for o in output.options:
        ctx = option_contexts.get(o.name)
        fact = facts.get(fact_id_for(ctx)) if ctx else None
        if fact is None:
            continue
        high = fact["levels"]["HIGH"]
        entry = {"option": o.name, "context": ctx, "fact_id": fact["fact_id"], "confidence_before": o.confidence,
                 "high_hit_rate": high["hit_rate"], "n": high["n"]}
        if o.confidence == "HIGH":
            if high["n"] < t["min_n"]:
                entry["result"] = f"insufficient history (n={high['n']})"
                notes.append(f"calibration: {o.name} insufficient history (n={high['n']}) for {ctx}; HIGH kept")
            elif high["hit_rate"] is not None and high["hit_rate"] < t["high_min_hit_rate"]:
                o.confidence = "MED"
                entry["result"] = "downgraded to MED"
                notes.append(f"calibration downgrade: {o.name} HIGH -> MED (past HIGH calls in {ctx} right "
                             f"{round(high['hit_rate'] * 100)}%, n={high['n']}; below {round(t['high_min_hit_rate'] * 100)}%)")
            else:
                entry["result"] = "kept HIGH"
        else:
            entry["result"] = "not HIGH: rule not applied"
        entry["confidence_after"] = o.confidence
        details.append(entry)
    return notes, details
