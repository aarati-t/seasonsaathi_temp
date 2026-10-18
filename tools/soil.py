"""Tool: get_soil_status(hh_id) (P7). Facts only: OpenLandMap soil at the plot point, SMAP soil moisture for the
village, and the soil type in the farmer's own words. Nothing is inferred; missing data becomes an 'unavailable' fact."""
from functools import lru_cache

import yaml

from core.settings import ROOT
from repo.base import Repo, get_repo

SMAP_SOURCE = "SMAP L4 soil moisture (NASA/SMAP/SPL4SMGP/008) via Earth Engine"
SMAP_URL = "https://developers.google.com/earth-engine/datasets/catalog/NASA_SMAP_SPL4SMGP_008"


@lru_cache(maxsize=1)
def soil_water() -> dict:
    """config/soil_water.yaml: FAO-56 Table 19 per texture class + farmer soil names -> texture class."""
    return yaml.safe_load((ROOT / "config" / "soil_water.yaml").read_text())


def plot_soil(hh_id: str, repo: Repo) -> dict | None:
    for p in repo.fetch_where("plots", hh_id=hh_id):
        rows = repo.fetch_where("soil_props", plot_id=p["plot_id"])
        if rows:
            return rows[0]
    return None


def latest_moisture(village_id: int, repo: Repo) -> dict | None:
    rows = [r for r in repo.fetch_where("soil_moisture", village_id=village_id) if r.get("sm_rootzone") is not None]
    return max(rows, key=lambda r: str(r["date"])) if rows else None


def texture_for(hh: dict, soil: dict | None) -> tuple[str | None, str | None]:
    """(texture class, fact_id it comes from). The soil map wins; otherwise the farmer's words via the config map."""
    if soil and soil.get("texture_class"):
        return soil["texture_class"], "F-soil-texture"
    mapped = soil_water()["farmer_soil"].get((hh.get("soil_type_farmer") or "").strip().lower())
    return (mapped, "F-soil-farmer") if mapped else (None, None)


def _pct(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else "unknown"


def get_soil_status(hh_id: str, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    rows = repo.fetch_where("households", hh_id=hh_id)
    if not rows:
        raise KeyError(f"household {hh_id} not found")
    hh = rows[0]
    facts = []
    soil = plot_soil(hh_id, repo)
    if soil:
        base = {"source": soil["source"], "source_url": soil["source_url"], "is_synthetic": bool(soil["is_synthetic"])}
        where = "plot (location SYNTHETIC)" if soil["is_synthetic"] else "plot"
        facts += [
            {**base, "fact_id": "F-soil-texture", "value": soil["texture_class"], "unit": None,
             "text": f"Soil at the {where}, soil map 0-30 cm: {soil['texture_class'] or 'texture unknown'}; "
                     f"clay {_pct(soil['clay_pct'])}%, sand {_pct(soil['sand_pct'])}%"},
            {**base, "fact_id": "F-soil-fc", "value": soil["water_fc_pct"], "unit": "% by volume",
             "text": f"Water the soil holds at field capacity (0-30 cm): {_pct(soil['water_fc_pct'])}% by volume"},
            {**base, "fact_id": "F-soil-oc", "value": soil["organic_carbon_pct"], "unit": "%",
             "text": f"Soil organic carbon (0-30 cm): {_pct(soil['organic_carbon_pct'])}%"},
            {**base, "fact_id": "F-soil-ph", "value": soil["ph"], "unit": "pH",
             "text": f"Soil pH in water (0-30 cm): {_pct(soil['ph'])}"},
        ]
    else:
        reason = ("no plot location on record" if not repo.fetch_where("plots", hh_id=hh_id)
                  else "no soil map row for this plot yet")
        facts.append({"fact_id": "F-soil-map-unavailable", "value": None, "unit": None, "source": "soil_props table",
                      "source_url": None, "is_synthetic": False, "text": f"Soil map values unavailable: {reason}"})
    if hh.get("soil_type_farmer"):
        synthetic = bool(hh["is_synthetic"])
        facts.append({"fact_id": "F-soil-farmer", "value": hh["soil_type_farmer"], "unit": None,
                      "source": "household profile (SYNTHETIC)" if synthetic else "household profile (farmer's words)",
                      "source_url": None, "is_synthetic": synthetic,
                      "text": f"Soil in the farmer's words: {hh['soil_type_farmer']}"})
    m = latest_moisture(hh["village_id"], repo)
    if m:
        base = {"source": SMAP_SOURCE, "source_url": SMAP_URL, "is_synthetic": False, "as_of": str(m["date"])}
        root = m["sm_rootzone"]
        facts.append({**base, "fact_id": "F-soil-rootzone", "value": round(root, 3), "unit": "m3/m3",
                      "text": f"Root-zone soil moisture (0-100 cm, village mean, week of {m['date']}): "
                              f"{round(root, 3)} m3/m3 ({round(root * 100, 1)}% by volume)"})
        if m.get("sm_surface") is not None:
            facts.append({**base, "fact_id": "F-soil-surface", "value": round(m["sm_surface"], 3), "unit": "m3/m3",
                          "text": f"Topsoil moisture (0-5 cm, week of {m['date']}): {round(m['sm_surface'], 3)} m3/m3"})
    else:
        facts.append({"fact_id": "F-soil-moisture-unavailable", "value": None, "unit": None, "source": SMAP_SOURCE,
                      "source_url": SMAP_URL, "is_synthetic": False,
                      "text": "Root-zone soil moisture unavailable for this village (no SMAP root-zone rows)"})
    return facts
