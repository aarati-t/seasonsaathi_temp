"""Price tools. Return facts only (value + fact_id + source), never a decision.

get_prices(commodity, district): crop facts from price_summary. Every fact carries fact_id, value,
  reference_type, reference_season, source and source_url for the Agent Trace. With no mandi data
  (e.g. safflower off-season) only the MSP fact is returned, and it says mandi data is unavailable.
get_livestock_price_facts(item_keys): fodder/cattle facts from livestock_prices, by item_key.
  basis=assumption rows are SYNTHETIC. Fodder camp rates carry the 2019 caveat.
get_fodder_intake(hh): intake norm; the 18 kg/day fodder camp ration unless the profile says otherwise.
"""
from urllib.parse import urlparse

from repo.base import Repo, get_repo


def _rs(v: float) -> str:
    return f"₹{v:,.0f}"


def _fact(fact_id: str, value, text: str, row: dict, source: str, source_url: str, **extra) -> dict:
    return {
        "fact_id": fact_id,
        "value": value,
        "unit": extra.pop("unit", "Rs/qtl"),
        "text": text,
        "reference_type": row["reference_type"],
        "reference_season": row["reference_season"],
        "source": source,
        "source_url": source_url,
        "is_synthetic": bool(row["is_synthetic"]),
        **extra,
    }


def get_prices(commodity: str, district: str, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    rows = repo.fetch_where("price_summary", commodity=commodity, district=district)
    base = f"F-price-{commodity.lower()}-{district.lower()}"
    if not rows:
        return [{"fact_id": f"{base}-none", "value": None, "unit": None,
                 "text": f"No price data for {commodity} in {district}", "reference_type": None,
                 "reference_season": None, "source": "price_summary", "source_url": None,
                 "is_synthetic": False, "mandi_available": False}]
    row = rows[0]
    ref, season = row["reference_price"], row["reference_season"]
    msp_text = f"MSP {_rs(ref)}/qtl ({season})"

    if row["latest_modal_price"] is None:
        return [_fact(f"{base}-msp", ref, f"{msp_text}; current mandi price unavailable", row,
                      "PIB", row["reference_source_url"], mandi_available=False, note=row["note"])]

    price, date = row["latest_modal_price"], str(row["latest_date"])
    pct = row["pct_vs_reference"]
    return [
        _fact(f"{base}-mandi", price, f"Agmarknet price {_rs(price)}/qtl in {district} on {date} "
                                      f"({pct:+.1f}% vs {msp_text})", row,
              row["source"], row["source_url"], as_of=date, mandi_available=True, note=row["note"]),
        _fact(f"{base}-msp", ref, msp_text, row, "PIB", row["reference_source_url"]),
        _fact(f"{base}-vs-msp", pct, f"Mandi price {_rs(price)}/qtl is {pct:+.1f}% vs {msp_text}", row,
              "computed from Agmarknet + PIB", row["source_url"], unit="%", as_of=date),
    ]


DEFAULT_INTAKE_KEY = "fodder_intake_large_animal_day"
FODDER_CAMP_PREFIX = "fodder_camp_"
FODDER_CAMP_CAVEAT = "2019 rate; current rate not confirmed"


def _num(v: float) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}".rstrip("0").rstrip(".")


def _livestock_fact(row: dict) -> dict:
    low, high, mid = row["value_low"], row["value_high"], row["value_mid"]
    synthetic = bool(row["is_synthetic"]) or row["basis"] == "assumption"
    text = f"{row['item']}: {_num(mid)} {row['unit']}"
    if low is not None and high is not None and low != high:
        text += f" (range {_num(low)}-{_num(high)})"
    text += f"; {row['region']}, {row['period']}"
    caveat = FODDER_CAMP_CAVEAT if row["item_key"].startswith(FODDER_CAMP_PREFIX) else None
    if caveat:
        text += f"; {caveat}"
    if synthetic:
        text += "; SYNTHETIC assumption"
    return {
        "fact_id": f"F-live-{row['item_key']}",
        "value": mid,
        "value_mid": mid,
        "range": [low, high],
        "unit": row["unit"],
        "period": row["period"],
        "text": text,
        "basis": row["basis"],
        "source": urlparse(row["source_url"]).netloc.removeprefix("www.") if row["source_url"] else "assumption (no source)",
        "source_url": row["source_url"],
        "is_synthetic": synthetic,
        "caveat": caveat,
        "note": row["note"],
    }


def get_livestock_price_facts(item_keys: list[str], repo: Repo | None = None) -> list[dict]:
    """One fact per known item_key, in the order asked. Unknown keys get a 'no data' fact."""
    repo = repo or get_repo()
    facts = []
    for key in item_keys:
        rows = repo.fetch_where("livestock_prices", item_key=key)
        facts.append(_livestock_fact(rows[0]) if rows else {
            "fact_id": f"F-live-{key}-none", "value": None, "value_mid": None, "range": None, "unit": None,
            "period": None, "text": f"No livestock price data for '{key}'", "basis": None, "source": None,
            "source_url": None, "is_synthetic": False, "caveat": None, "note": None,
        })
    return facts


def get_fodder_intake(hh: dict | None = None, repo: Repo | None = None) -> dict:
    """Intake per large animal per day. Uses the profile's fodder_intake_kg_per_day when given."""
    own = (hh or {}).get("fodder_intake_kg_per_day")
    if own is not None:
        return {"fact_id": f"F-hh-{hh['hh_id']}-intake", "value": own, "value_mid": own, "range": [own, own],
                "unit": "kg per animal per day", "period": None,
                "text": f"Household reports {_num(own)} kg fodder per animal per day", "basis": "reported",
                "source": "household profile", "source_url": None, "is_synthetic": bool(hh.get("is_synthetic")),
                "caveat": None, "note": None}
    fact = get_livestock_price_facts([DEFAULT_INTAKE_KEY], repo)[0]
    fact["text"] = f"Default intake norm (household did not say): {fact['text']}"
    return fact
