"""Tool: get_plot_stress_facts(plot_id) for F2. All numbers computed here, in code, with their sources:
cum_rain_deficit_pct (village, CHIRPS), longest_dry_spell_days (dry day = rain < rain.dry_day_mm),
ndvi_pct_of_normal (latest cloud-free Sentinel-2 window), data_as_of (last CHIRPS date).
stress_level() applies the F2 thresholds in code: HIGH if >= 2 crossed, MED if 1, LOW if 0.
"""
from datetime import date, timedelta

from core.settings import thresholds
from repo.base import Repo, get_repo
from tools.ndvi import SOURCE as NDVI_SOURCE, SOURCE_URL as NDVI_URL
from tools.rain import SOURCE as RAIN_SOURCE, SOURCE_URL as RAIN_URL, dry_spells


NDVI_WINDOW_DAYS = 10   # ndvi_series rows are 10-day composites; date = window start (scripts/ee_export.py)


def get_plot_stress_facts(plot_id: str, repo: Repo | None = None, cutoff: str | None = None) -> list[dict]:
    """cutoff (YYYY-MM-DD, P5b K3 replay): use only rain days <= cutoff and NDVI windows that END by the cutoff,
    so no later observation can influence the result. Normals are 2014-2023 / 2019-2023 (earlier years only)."""
    repo = repo or get_repo()
    t = thresholds()
    plots = repo.fetch_where("plots", plot_id=plot_id)
    if not plots:
        raise KeyError(f"plot {plot_id} not found")
    plot = plots[0]
    hh = repo.fetch_where("households", hh_id=plot["hh_id"])[0]
    rows = sorted(repo.fetch_where("rain_series", village_id=hh["village_id"]), key=lambda r: str(r["date"]))
    if cutoff:
        rows = [r for r in rows if str(r["date"]) <= cutoff]
    facts = []
    rain_synth = any((r.get("source") or "") == "SYNTHETIC" for r in rows)
    base = {"source": RAIN_SOURCE, "source_url": RAIN_URL, "is_synthetic": rain_synth}
    if rows:
        as_of = str(rows[-1]["date"])
        deficit_rows = [r for r in rows if r["cum_deficit_pct"] is not None]
        deficit = deficit_rows[-1]["cum_deficit_pct"] if deficit_rows else None
        normal_so_far = sum(r["normal_mm"] for r in rows if r["normal_mm"] is not None)
        if normal_so_far < t["rain"]["min_normal_mm_for_deficit"]:
            deficit = None   # same rule as tools/rain.py: too little normal rain yet for a meaningful deficit %
        longest, _current = dry_spells([r["rain_mm"] for r in rows], t["rain"]["dry_day_mm"])
        facts += [
            {**base, "fact_id": "F-stress-asof", "name": "data_as_of", "value": as_of, "unit": "date",
             "text": f"Rain data up to {as_of} (last available CHIRPS date)"},
            {**base, "fact_id": "F-stress-deficit", "name": "cum_rain_deficit_pct", "value": deficit, "unit": "% below normal",
             "as_of": as_of, "text": f"Season rain is {deficit}% below normal (village, since {rows[0]['date']}, to {as_of})"
             if deficit is not None else "Season rain deficit unknown"},
            {**base, "fact_id": "F-stress-dryspell", "name": "longest_dry_spell_days", "value": longest, "unit": "days",
             "as_of": as_of, "text": f"Longest dry spell: {longest} days (days with rain under {t['rain']['dry_day_mm']} mm), to {as_of}"},
        ]
    else:
        facts.append({**base, "fact_id": "F-stress-asof", "name": "data_as_of", "value": None, "unit": "date",
                      "text": "No rainfall data for this village"})
    ndvi = [r for r in sorted(repo.fetch_where("ndvi_series", plot_id=plot_id), key=lambda r: str(r["date"]))
            if r["ndvi"] is not None and r["ndvi_normal"]
            and (not cutoff or str(date.fromisoformat(str(r["date"])[:10]) + timedelta(days=NDVI_WINDOW_DAYS - 1)) <= cutoff)]
    nbase = {"source": NDVI_SOURCE, "source_url": NDVI_URL, "is_synthetic": bool(plot["is_synthetic"])}
    if ndvi:
        r = ndvi[-1]
        pct = round(r["ndvi"] / r["ndvi_normal"] * 100, 1)
        facts.append({**nbase, "fact_id": "F-stress-ndvi", "name": "ndvi_pct_of_normal", "value": pct, "unit": "% of normal",
                      "as_of": str(r["date"]), "text": f"Crop greenness (NDVI) is {pct}% of normal for the 10 days from {r['date']}"})
    else:
        facts.append({**nbase, "fact_id": "F-stress-ndvi", "name": "ndvi_pct_of_normal", "value": None, "unit": "% of normal",
                      "text": "No cloud-free NDVI for this plot"})
    return facts


def stress_level(facts: list[dict]) -> tuple[str, list[str]]:
    """(level, crossed thresholds). Unknown values never count as crossed."""
    t = thresholds()
    v = {f["fact_id"]: f.get("value") for f in facts}
    crossed = []
    if v.get("F-stress-deficit") is not None and v["F-stress-deficit"] > t["rain"]["deficit_pct_alert"]:
        crossed.append(f"rain deficit {v['F-stress-deficit']}% > {t['rain']['deficit_pct_alert']}%")
    if v.get("F-stress-dryspell") is not None and v["F-stress-dryspell"] > t["rain"]["dry_spell_days_alert"]:
        crossed.append(f"dry spell {v['F-stress-dryspell']} days > {t['rain']['dry_spell_days_alert']} days")
    if v.get("F-stress-ndvi") is not None and v["F-stress-ndvi"] < t["ndvi"]["pct_of_normal_alert"]:
        crossed.append(f"NDVI {v['F-stress-ndvi']}% < {t['ndvi']['pct_of_normal_alert']}% of normal")
    level = "HIGH" if len(crossed) >= 2 else "MED" if len(crossed) == 1 else "LOW"
    return level, crossed
