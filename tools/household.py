"""Household profile facts (input to F3/F4). Facts only: one per known profile field."""
from core.settings import thresholds
from features.f1_interview import flags_for, profile_completeness
from repo.base import Repo, get_repo

FIELDS = [
    ("irrigation", "Irrigation source", None),
    ("water_hours_per_week", "Water available", "hours per week"),
    ("cash_buffer_band", "Cash buffer", None),
    ("acres", "Land", "acres"),
    ("tenure", "Tenure", None),
    ("crops_last_season", "Kharif crops last season", None),
    ("cattle_count", "Cattle", "animals"),
    ("goats_count", "Goats", "animals"),
    ("fodder_weeks_left", "Fodder left", "weeks"),
    ("off_farm_income", "Off-farm income", None),
    ("loan_due_month", "Loan due month", "month number"),
    ("primary_cultivator_gender", "Primary cultivator", None),
    ("soil_type_farmer", "Soil (farmer's words)", None),
]


def load_household(hh_id: str, repo: Repo | None = None) -> tuple[dict, dict]:
    """(household row, village row). Raises KeyError if the household does not exist."""
    repo = repo or get_repo()
    rows = repo.fetch_where("households", hh_id=hh_id)
    if not rows:
        raise KeyError(f"household {hh_id} not found")
    hh = rows[0]
    village = repo.fetch_where("villages", village_id=hh["village_id"])[0]
    return hh, village


def household_facts(hh: dict, village: dict) -> list[dict]:
    synthetic = bool(hh["is_synthetic"])
    source = "household profile (SYNTHETIC)" if synthetic else "household profile (survey)"
    facts = []
    for field, label, unit in FIELDS:
        value = hh.get(field)
        if value is None and field == "soil_type_farmer":   # optional (P7): no fact unless the farmer stated it
            continue
        if value is None:
            text = f"{label}: unknown"
        else:
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            text = f"{label}: {value}" + (f" {unit}" if unit else "")
        if field == "fodder_weeks_left" and value is not None:
            text += f" for {hh.get('cattle_count') or 0} cattle and {hh.get('goats_count') or 0} goats"
        facts.append({"fact_id": f"F-hh-{field}", "value": value, "unit": unit, "text": text,
                      "source": source, "source_url": None, "is_synthetic": synthetic})
    pct, floor = profile_completeness(hh), thresholds()["profile"]["min_completeness_pct"]
    facts.append({"fact_id": "F-hh-completeness", "value": pct, "unit": "% complete (weighted)",
                  "flags": flags_for(pct),
                  "text": f"Profile completeness: {pct}% (weighted)"
                          + (f"; profile incomplete (below {floor}%), confidence capped at LOW" if pct < floor else ""),
                  "source": source, "source_url": None, "is_synthetic": synthetic})
    facts.append({"fact_id": "F-hh-village", "value": village["name"], "unit": None,
                  "text": f"Village: {village['name']}, {village['taluka']} taluka, {village['district']} district",
                  "source": "data/villages.csv (hand-entered)", "source_url": None, "is_synthetic": False})
    return facts
