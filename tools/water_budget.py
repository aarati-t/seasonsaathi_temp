"""Tool: get_water_budget(hh_id) (P7, SPEC F3 step 1). All arithmetic in code; facts only, no decision.

Per F3 option: coverage % = water available / crop water need x 100, as a low-high range.
  water available = plant-available root-zone water now + irrigation over the rabi season
  root-zone water = (SMAP root-zone moisture - wilting point) x root depth, capped at (field capacity - wilting point)
                    x root depth; wilting point / field capacity from FAO-56 Table 19 for the soil texture
  irrigation      = water hours/week x rabi weeks x pump flow / plot area (pump flow is a labelled assumption)
  need            = crop_water_need (cited sources); a crop mix uses the larger need of its crops (conservative)
Any missing input -> that option's coverage is 'not available' with the reason; nothing is filled in.
"""
from core.settings import thresholds
from repo.base import Repo, get_repo
from tools.soil import latest_moisture, plot_soil, soil_water, texture_for

OPTION_CROPS = {"Chana only": ["gram"], "Chana + safflower": ["gram", "safflower"], "Jowar + fodder": ["sorghum"]}
SQ_M_PER_ACRE = 4046.86
SEASON = "rabi"


def option_key(name: str) -> str:
    return "-".join(OPTION_CROPS[name])


def coverage_fact_id(name: str) -> str:
    return f"F-wb-coverage-{option_key(name)}"


def band(pct: float) -> str:
    t = thresholds()["water_budget"]
    return "fit" if pct >= t["fit_min_pct"] else "tight" if pct >= t["tight_min_pct"] else "short"


def allowed_bands(low: float, high: float) -> list[str]:
    """water_fit values consistent with a coverage range (both ends and everything between)."""
    order = ["short", "tight", "fit"]
    return order[order.index(band(low)):order.index(band(high)) + 1]


def irrigation_mm(hh: dict) -> tuple[float | None, str, bool]:
    """(mm over the season or None, explanation, uses the pump-flow assumption)."""
    t = thresholds()["water_budget"]
    if hh.get("irrigation") == "rainfed":
        return 0.0, "rainfed: no irrigation water", False
    hours, acres = hh.get("water_hours_per_week"), hh.get("acres")
    if hh.get("irrigation") is None or hours is None:
        return None, "irrigation source or water hours unknown", False
    if not acres:
        return None, "land area unknown", False
    m3 = hours * t["rabi_irrigation_weeks"] * t["pump_m3_per_hour"]
    mm = round(m3 / (acres * SQ_M_PER_ACRE) * 1000)
    return mm, (f"{hours:g} h/week x {t['rabi_irrigation_weeks']} weeks x {t['pump_m3_per_hour']} m3/h pump "
                f"(assumption) over {acres:g} acres"), True


def stored_mm(theta: float, tex: dict, depth_m: float) -> tuple[int, int]:
    """Plant-available root-zone water now (low, high) in mm over depth_m."""
    (fc_lo, fc_hi), (wp_lo, wp_hi) = tex["theta_fc"], tex["theta_wp"]
    low = min(max(0.0, theta - wp_hi), max(0.0, fc_lo - wp_hi)) * depth_m * 1000
    high = min(max(0.0, theta - wp_lo), max(0.0, fc_hi - wp_lo)) * depth_m * 1000
    return round(low), round(high)


def get_water_budget(hh_id: str, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    rows = repo.fetch_where("households", hh_id=hh_id)
    if not rows:
        raise KeyError(f"household {hh_id} not found")
    hh = rows[0]
    t = thresholds()["water_budget"]
    need = {r["crop_key"]: r for r in repo.fetch_where("crop_water_need", season=SEASON)}
    facts = []

    irr, irr_how, assumed = irrigation_mm(hh)
    facts.append({"fact_id": "F-wb-irrigation", "value": irr, "unit": "mm over the rabi season",
                  "source": "computed in code from the household profile" + (" + pump-flow assumption" if assumed else ""),
                  "source_url": None, "is_synthetic": assumed or bool(hh["is_synthetic"]),
                  "basis": "assumption" if assumed else "derived",
                  "text": f"Irrigation water over the rabi season: {f'about {irr:g} mm' if irr is not None else 'unknown'} ({irr_how})"})

    for crop in sorted({c for cs in OPTION_CROPS.values() for c in cs}):
        r = need.get(crop)
        if r is None:
            continue
        rng = [r["need_mm_low"], r["need_mm_high"]]
        amount = f"{rng[0]:g}" if rng[0] == rng[1] else f"{rng[0]:g}-{rng[1]:g}"
        facts.append({"fact_id": f"F-wb-need-{crop}", "value": None if rng[0] != rng[1] else rng[0], "range": rng,
                      "unit": "mm per season", "source": f"{r['note']}"[:300], "source_url": r["source_url"],
                      "is_synthetic": bool(r["is_synthetic"]), "basis": r["basis"],
                      "caveat": None if "Marathwada" in (r["region"] or "") else f"measured in {r['region']}",
                      "text": f"{r['crop']} needs about {amount} mm of water over the {SEASON} season ({r['region']})"})

    soil = plot_soil(hh_id, repo)
    texture, texture_fact = texture_for(hh, soil)
    tex = soil_water()["texture"].get(texture) if texture else None
    moisture = latest_moisture(hh["village_id"], repo)
    synthetic_inputs = assumed or bool(hh["is_synthetic"]) or bool(soil and soil["is_synthetic"])

    for name, crops in OPTION_CROPS.items():
        fid, reasons = coverage_fact_id(name), []
        missing = [c for c in crops if c not in need]
        if missing:
            reasons.append(f"no sourced water need for {', '.join(missing)}")
        if texture is None:
            reasons.append("soil texture unknown (no soil map for this plot and no soil type from the farmer)")
        elif tex is None:
            reasons.append(f"soil texture '{texture}' is not in FAO-56 Table 19")
        if moisture is None:
            reasons.append("no root-zone soil moisture for the village")
        if irr is None:
            reasons.append(f"irrigation unknown ({irr_how})")
        base = {"fact_id": fid, "unit": "% of crop water need", "source": "computed in code (P7 water budget)",
                "source_url": None, "is_synthetic": synthetic_inputs or any(need[c]["is_synthetic"] for c in crops if c in need)}
        if reasons:
            facts.append({**base, "value": None, "available": False,
                          "text": f"{name}: water coverage not available ({'; '.join(reasons)})"})
            continue
        depth = min(min(need[c]["root_depth_m_low"] for c in crops), t["smap_rootzone_depth_m"])
        s_lo, s_hi = stored_mm(moisture["sm_rootzone"], tex, depth)
        n_lo = max(need[c]["need_mm_low"] for c in crops)
        n_hi = max(need[c]["need_mm_high"] for c in crops)
        lo, hi = round((s_lo + irr) / n_hi * 100), round((s_hi + irr) / n_lo * 100)
        cov = f"{lo}" if lo == hi else f"{lo}-{hi}"
        need_txt = f"{n_lo:g}" if n_lo == n_hi else f"{n_lo:g}-{n_hi:g}"
        facts.append({**base, "value": None if lo != hi else lo, "range": [lo, hi], "available": True,
                      "inputs": {"stored_mm": [s_lo, s_hi], "irrigation_mm": irr, "need_mm": [n_lo, n_hi],
                                 "root_depth_m": depth, "texture": texture, "texture_fact": texture_fact,
                                 "rootzone_m3m3": moisture["sm_rootzone"], "as_of": str(moisture["date"])},
                      "as_of": str(moisture["date"]),
                      "text": f"{name}: water available covers about {cov}% of the crop's need "
                              f"(root-zone water {s_lo}-{s_hi} mm in {depth:g} m of {texture} soil + irrigation "
                              f"{irr:g} mm, against a need of {need_txt} mm)"})
    return facts
