"""Tool: get_rain_status(village_id). Facts from rain_series (CHIRPS via Earth Engine). No decisions."""
from core.settings import thresholds
from repo.base import Repo, get_repo

SOURCE = "CHIRPS daily (UCSB-CHG/CHIRPS/DAILY) via Earth Engine"
SOURCE_URL = "https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY"


def dry_spells(rain_mm: list[float | None], dry_day_mm: float) -> tuple[int, int]:
    """(longest, current) run of dry days. A missing day ends a run (it is unknown, not dry)."""
    longest = run = 0
    for v in rain_mm:
        run = run + 1 if v is not None and v < dry_day_mm else 0
        longest = max(longest, run)
    return longest, run


def get_rain_status(village_id: int, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    t = thresholds()["rain"]
    rows = sorted(repo.fetch_where("rain_series", village_id=village_id), key=lambda r: r["date"])
    data_source = sorted({r.get("source") or "unknown" for r in rows}) if rows else ["unknown"]
    synthetic = "SYNTHETIC" in data_source
    base = {"source": f"SYNTHETIC rain (not {SOURCE})" if synthetic else SOURCE, "source_url": SOURCE_URL,
            "is_synthetic": synthetic, "data_source": "/".join(data_source)}
    if not rows:
        return [{**base, "fact_id": "F-rain-none", "value": None, "unit": None,
                 "text": f"No rainfall data for village {village_id}"}]
    first, last = str(rows[0]["date"]), str(rows[-1]["date"])
    rain = [r["rain_mm"] for r in rows]
    total = round(sum(v for v in rain if v is not None), 1)
    normal = round(sum(r["normal_mm"] for r in rows if r["normal_mm"] is not None), 1)
    longest, current = dry_spells(rain, t["dry_day_mm"])
    facts = [
        {**base, "fact_id": "F-rain-total", "value": total, "unit": "mm", "as_of": last,
         "text": f"Rain {first} to {last}: {total} mm against a normal of {normal} mm"},
        {**base, "fact_id": "F-rain-dryspell", "value": longest, "unit": "days", "as_of": last,
         "text": f"Longest dry spell since {first}: {longest} days (days under {t['dry_day_mm']} mm)"},
        {**base, "fact_id": "F-rain-current-dry", "value": current, "unit": "days", "as_of": last,
         "text": f"Dry days in a row up to {last}: {current}"},
    ]
    deficit_rows = [r for r in rows if r["cum_deficit_pct"] is not None]
    if deficit_rows and normal >= t["min_normal_mm_for_deficit"]:
        d = deficit_rows[-1]["cum_deficit_pct"]
        word = "below" if d >= 0 else "above"
        facts.insert(0, {**base, "fact_id": "F-rain-deficit", "value": d, "unit": "% below normal",
                         "as_of": str(deficit_rows[-1]["date"]),
                         "text": f"Season rain is {abs(d)}% {word} normal (cumulative since {first}, to {deficit_rows[-1]['date']})"})
    return facts
