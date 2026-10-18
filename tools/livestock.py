"""Tool: get_livestock_facts(hh_id) for F4. ALL arithmetic (kg, weeks, ₹) happens here, in code, and is
returned as facts {fact_id, name, value, unit, source, source_url, is_synthetic}. Gemini only compares them.

Prices and norms come from livestock_prices (data/raw/livestock_prices.csv, values unchanged):
  kadba_kg_now / kadba_kg_normal (₹/kg), cattle_price_drop_pct, cow_sale_now_example,
  desi_cow_normal_value (SYNTHETIC assumption), silage_*_kg, fodder_camp_large_animal_day (2019 rate).
Config (config/thresholds.yaml f4): fodder_horizon_weeks (assumption), intake_kg_per_day_large (sourced).
cattle_count covers cows and buffaloes; the split is unknown, so cow values are used for both.
"""
import math

from core.settings import thresholds
from repo.base import Repo, get_repo

OPTIONS_ONE = ["KEEP_AND_BUY_FODDER", "KEEP_WITH_SUPPORT", "SELL_NOW"]
OPTIONS_MANY = ["KEEP_AND_BUY_FODDER", "SELL_ONE_KEEP_REST", "SELL_NOW"]
CAMP_CAVEAT = "2019 rate; current rate not confirmed"


def option_set(cattle_count: int | None) -> list[str]:
    if not cattle_count:
        return []
    return OPTIONS_ONE if cattle_count == 1 else OPTIONS_MANY


def shortfall_weeks(horizon: float, fodder_weeks_left: float | None) -> float | None:
    return None if fodder_weeks_left is None else max(0.0, horizon - fodder_weeks_left)


def fodder_kg(animals: int, intake_kg_day: float, weeks: float) -> float:
    return animals * intake_kg_day * 7 * weeks


def money(x: float | None) -> int | None:
    return None if x is None else int(round(x))


def rs(x) -> str:
    return "unknown" if x is None else f"₹{x:,.0f}"


def _row(rows: dict, key: str) -> dict:
    if key not in rows:
        raise KeyError(f"livestock_prices has no item_key {key!r} (load data/raw/livestock_prices.csv)")
    return rows[key]


def get_livestock_facts(hh_id: str, repo: Repo | None = None) -> list[dict]:
    from tools.household import load_household  # local import: avoids a cycle via features.f1_interview

    repo = repo or get_repo()
    t = thresholds()["f4"]
    hh, _village = load_household(hh_id, repo)
    rows = {r["item_key"]: r for r in repo.fetch_all("livestock_prices")}
    hh_src = "household profile (SYNTHETIC)" if hh["is_synthetic"] else "household profile (survey)"
    facts: list[dict] = []

    def add(fid, name, value, unit, text, source, source_url=None, synthetic=False, **extra):
        facts.append({"fact_id": fid, "name": name, "value": value, "unit": unit, "text": text, "source": source,
                      "source_url": source_url, "is_synthetic": bool(synthetic), **extra})

    def price_fact(fid, key, name, extra_text=""):
        r = _row(rows, key)
        rng = f" (range {r['value_low']:g}-{r['value_high']:g})" if r["value_low"] != r["value_high"] else ""
        add(fid, name, r["value_mid"], r["unit"], f"{name}: {r['value_mid']:g} {r['unit']}{rng}; {r['region']}, {r['period']}{extra_text}",
            r["source_url"] or "assumption (no source)", r["source_url"], r["is_synthetic"] or r["basis"] == "assumption",
            range=[r["value_low"], r["value_high"]], basis=r["basis"])
        return r

    animals = hh["cattle_count"]
    add("F-f4-animals", "large animals", animals, "animals",
        f"Large animals (cows + buffaloes): {animals if animals is not None else 'unknown'}", hh_src, synthetic=hh["is_synthetic"])
    if hh.get("goats_count"):
        add("F-f4-goats-note", "goats", hh["goats_count"], "goats",
            f"{hh['goats_count']} goats: goat fodder not costed – no sourced intake norm", hh_src, synthetic=hh["is_synthetic"])
    if not animals:
        return facts

    weeks = hh["fodder_weeks_left"]
    horizon = float(t["fodder_horizon_weeks"])
    intake_row = price_fact("F-f4-intake", "fodder_intake_large_animal_day", "fodder intake per large animal")
    intake = float(t["intake_kg_per_day_large"])
    short = shortfall_weeks(horizon, weeks)
    add("F-f4-fodder-weeks", "fodder weeks left", weeks, "weeks",
        f"Fodder left: {weeks if weeks is not None else 'unknown'} weeks", hh_src, synthetic=hh["is_synthetic"])
    add("F-f4-horizon", "fodder horizon", horizon, "weeks",
        f"Planning horizon: {horizon:g} weeks until new fodder (assumption)", "config/thresholds.yaml (assumption)",
        synthetic=True, assumption=True)
    add("F-f4-shortfall-weeks", "fodder shortfall", short, "weeks",
        f"Fodder shortfall: {short:g} weeks (horizon {horizon:g} − {weeks:g} left)" if short is not None
        else "Fodder shortfall: unknown (fodder weeks left not known)", "computed in code", synthetic=hh["is_synthetic"])

    now = price_fact("F-f4-kadba-now", "kadba_kg_now", "jowar kadba price now")
    normal = price_fact("F-f4-kadba-normal", "kadba_kg_normal", "jowar kadba price, normal season")
    drop = price_fact("F-f4-price-drop", "cattle_price_drop_pct", "cattle sale price change in drought")
    example = price_fact("F-f4-sale-example", "cow_sale_now_example", "cow sold in drought (market example)")
    norm_cow = price_fact("F-f4-sale-normal", "desi_cow_normal_value", "cow value in a normal year")
    price_fact("F-f4-silage-own", "silage_own_maize_kg", "maize silage (own maize)", "; price only; not intake-equivalent to dry kadba")
    price_fact("F-f4-silage-ready", "silage_ready_kg", "maize silage (ready-made)", "; price only; not intake-equivalent to dry kadba")
    camp = price_fact("F-f4-camp", "fodder_camp_large_animal_day", "fodder camp support", f"; {CAMP_CAVEAT}")
    facts[-1]["caveat"] = CAMP_CAVEAT

    # Sale value now per animal = normal value x (1 + drop%), drop range from the sourced -25..-20 %.
    nmid = norm_cow["value_mid"]
    sale = {k: nmid * (1 + drop[v] / 100) for k, v in (("low", "value_low"), ("mid", "value_mid"), ("high", "value_high"))}
    add("F-f4-sale-now", "sale value now per animal", money(sale["mid"]), "₹ per animal",
        f"Sale value now per animal: {rs(sale['mid'])} (range {rs(sale['low'])}-{rs(sale['high'])}) = normal {rs(nmid)} "
        f"with a {abs(drop['value_low']):g}-{abs(drop['value_high']):g}% drought drop", "computed: normal value (SYNTHETIC) x sourced drop",
        drop["source_url"], True, range=[money(sale["low"]), money(sale["high"])])
    loss = {k: nmid - v for k, v in sale.items()}
    add("F-f4-loss-per-animal", "loss if sold now per animal", money(loss["mid"]), "₹ per animal",
        f"Loss if one animal is sold now: {rs(loss['mid'])} (range {rs(loss['high'])}-{rs(loss['low'])}) below its normal value",
        "computed in code", None, True, range=[money(loss["high"]), money(loss["low"])])

    def keep_cost(n_keep: int) -> dict | None:
        if short is None:
            return None
        kg = fodder_kg(n_keep, intake, short)
        return {"kg": kg, **{k: kg * now[v] for k, v in (("low", "value_low"), ("mid", "value_mid"), ("high", "value_high"))}}

    full = keep_cost(animals)
    if full is not None:
        add("F-f4-fodder-cost", "fodder cost for the shortfall", money(full["mid"]), "₹",
            f"Buying kadba for {animals} animal(s) for {short:g} weeks: {full['kg']:,.0f} kg ({animals} × {intake:g} kg × 7 × {short:g}) "
            f"= {rs(full['mid'])} (range {rs(full['low'])}-{rs(full['high'])} at ₹{now['value_low']:g}-{now['value_high']:g}/kg)",
            "computed in code", None, hh["is_synthetic"], range=[money(full["low"]), money(full["high"])], kg=full["kg"])

    for opt in option_set(animals):
        keep = {"KEEP_AND_BUY_FODDER": animals, "KEEP_WITH_SUPPORT": animals, "SELL_ONE_KEEP_REST": animals - 1,
                "SELL_NOW": 0}[opt]
        sold = animals - keep
        cost = keep_cost(keep) if keep else {"kg": 0, "low": 0, "mid": 0, "high": 0}
        base = f"F-f4-opt-{opt}"
        if opt == "KEEP_WITH_SUPPORT":
            low, high = 0, (cost["high"] if cost else None)
            text = (f"{opt}: cash needed {rs(0)} if a fodder camp place is confirmed, about {rs(cost['mid'])} "
                    f"(up to {rs(high)}) if fodder must be bought for {keep} animal(s) for {short:g} weeks"
                    if cost else f"{opt}: cash needed unknown (fodder weeks left not known)")
            add(f"{base}-cash", f"{opt} cash needed", money(cost["mid"]) if cost else None, "₹", text, "computed in code", None,
                True, range=[0, money(high)], assumption="camp place availability not confirmed")
        else:
            text = (f"{opt}: cash needed {rs(cost['mid'])} (range {rs(cost['low'])}-{rs(cost['high'])}) for fodder for {keep} animal(s)"
                    + (f" for {short:g} weeks" if keep else "") if cost else f"{opt}: cash needed unknown (fodder weeks left not known)")
            add(f"{base}-cash", f"{opt} cash needed", money(cost["mid"]) if cost else None, "₹", text, "computed in code", None,
                hh["is_synthetic"], range=[money(cost["low"]), money(cost["high"])] if cost else None)
        add(f"{base}-asset", f"{opt} asset value kept", money(keep * nmid), "₹",
            f"{opt}: keeps {keep} animal(s) worth {rs(keep * nmid)} at normal value (SYNTHETIC normal value)",
            "computed: animals kept x normal value (SYNTHETIC)", None, True,
            range=[money(keep * norm_cow["value_low"]), money(keep * norm_cow["value_high"])], animals_kept=keep)
        if sold:
            add(f"{base}-cash-in", f"{opt} sale proceeds", money(sold * sale["mid"]), "₹",
                f"{opt}: selling {sold} animal(s) now brings about {rs(sold * sale['mid'])} "
                f"(range {rs(sold * sale['low'])}-{rs(sold * sale['high'])}), {rs(sold * loss['mid'])} below normal value",
                "computed in code", None, True, range=[money(sold * sale["low"]), money(sold * sale["high"])],
                loss=money(sold * loss["mid"]))
    _ = (intake_row, normal, example, camp)  # facts already added
    return facts


def weeks_needed_for_limit(animals: int, intake: float, horizon: float, price_per_kg: float, limit_rs: float) -> int:
    """Smallest whole fodder_weeks_left at which buying fodder for the rest of the horizon costs <= limit_rs."""
    per_week = animals * intake * 7 * price_per_kg
    if per_week <= 0:
        return 0
    return max(0, min(int(horizon), math.ceil(horizon - limit_rs / per_week)))
