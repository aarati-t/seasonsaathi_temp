"""Read-only views for the UI (SPEC section 9). They return stored rows and values the features already
compute; nothing here makes a decision or a new fact."""
import json

from features.f1_interview import flags_for, profile_completeness
from features.f4_livestock import support_options
from repo.base import Repo

HIDDEN = {"profile_embedding"}   # large float array; the UI never needs it


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in HIDDEN}


def _output(row: dict) -> dict:
    out = row.get("output_json")
    return out if isinstance(out, dict) else json.loads(out or "{}")


def _latest_f3(repo: Repo) -> dict[str, dict]:
    """Latest F3 result per household: recommended option, its signal and confidence (as logged)."""
    latest: dict[str, dict] = {}
    for r in repo.fetch_where("decision_log", feature="f3"):
        if r.get("hh_id") and r["request_id"] > latest.get(r["hh_id"], {}).get("request_id", ""):
            latest[r["hh_id"]] = r
    out = {}
    for hh_id, r in latest.items():
        o = _output(r)
        opt = next((x for x in o.get("options", []) if x.get("name") == o.get("recommended")), {})
        out[hh_id] = {"request_id": r["request_id"], "recommended": o.get("recommended"),
                      "signal": opt.get("signal"), "confidence": opt.get("confidence"),
                      "verifier_status": r.get("verifier_status")}
    return out


def _latest_alert(repo: Repo) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for a in repo.fetch_all("alerts"):
        if a.get("hh_id") and str(a.get("created_at")) > str(latest.get(a["hh_id"], {}).get("created_at", "")):
            latest[a["hh_id"]] = a
    return {h: {"alert_id": a["alert_id"], "stress_level": a.get("stress_level"), "next_step": a.get("next_step"),
                "created_at": str(a.get("created_at")), "via": a.get("via")} for h, a in latest.items()}


def households(repo: Repo) -> list[dict]:
    villages = {v["village_id"]: v for v in repo.fetch_all("villages")}
    plots: dict[str, list[str]] = {}
    for p in repo.fetch_all("plots"):
        plots.setdefault(p["hh_id"], []).append(p["plot_id"])
    f3, alerts = _latest_f3(repo), _latest_alert(repo)
    rows = []
    for hh in repo.fetch_all("households"):
        pct = profile_completeness(hh)
        v = villages.get(hh["village_id"], {})
        rows.append({"hh_id": hh["hh_id"], "village_id": hh["village_id"], "village": v.get("name"),
                     "tenure": hh.get("tenure"), "cattle_count": hh.get("cattle_count"),
                     "is_synthetic": bool(hh.get("is_synthetic")), "completeness_pct": pct, "flags": flags_for(pct),
                     "created_at": str(hh["created_at"]) if hh.get("created_at") else None,
                     "plot_ids": sorted(plots.get(hh["hh_id"], [])), "is_demo": hh["hh_id"].startswith("HH-DEMO-"),
                     "latest_f3": f3.get(hh["hh_id"]), "latest_alert": alerts.get(hh["hh_id"])})
    return sorted(rows, key=lambda r: (not r["is_demo"], r["hh_id"]))


def household(repo: Repo, hh_id: str) -> dict:
    rows = repo.fetch_where("households", hh_id=hh_id)
    if not rows:
        raise KeyError(f"household {hh_id} not found")
    hh = _clean(rows[0])
    hh["created_at"] = str(hh["created_at"]) if hh.get("created_at") else None
    village = next(iter(repo.fetch_where("villages", village_id=hh["village_id"])), None)
    pct = profile_completeness(hh)
    return {"household": hh, "village": village, "completeness_pct": pct, "flags": flags_for(pct),
            "plots": [_clean(p) for p in repo.fetch_where("plots", hh_id=hh_id)],
            "latest_f3": _latest_f3(repo).get(hh_id)}


def plot_series(repo: Repo, plot_id: str) -> dict:
    """Stored rain (village) and NDVI (plot) rows, each with its source column; no values are derived."""
    plot = next(iter(repo.fetch_where("plots", plot_id=plot_id)), None)
    if plot is None:
        raise KeyError(f"plot {plot_id} not found")
    hh = repo.fetch_where("households", hh_id=plot["hh_id"])[0]
    rain = sorted(repo.fetch_where("rain_series", village_id=hh["village_id"]), key=lambda r: str(r["date"]))
    ndvi = sorted(repo.fetch_where("ndvi_series", plot_id=plot_id), key=lambda r: str(r["date"]))
    return {"plot_id": plot_id, "hh_id": plot["hh_id"], "plot_is_synthetic": bool(plot.get("is_synthetic")),
            "rain": [{**r, "date": str(r["date"])} for r in rain], "ndvi": [{**r, "date": str(r["date"])} for r in ndvi]}


def support() -> dict:
    """config/support_options.yaml as-is (hand-checked pointers; F4 cites them by id)."""
    return support_options()


def ui_config() -> dict:
    """Thresholds the UI shows as hints or uses to highlight rows (config/thresholds.yaml, unchanged)."""
    from core.settings import thresholds
    from features.learning import RESULTS, TERM_CATEGORIES
    t = thresholds()
    return {"profile": t["profile"], "calibration": t["calibration"], "f2": {"rain": t["rain"], "ndvi": t["ndvi"]},
            "term_categories": sorted(TERM_CATEGORIES), "outcome_results": sorted(RESULTS)}
