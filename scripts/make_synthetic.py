"""Synthetic households, plots and outcomes. EVERY row is is_synthetic=true.

Usage: python scripts/make_synthetic.py --n 25 --seed 42
Reads:  data/villages.csv (hand-entered; never generated)
Writes: data/households.csv, data/plots.csv, data/outcomes.csv

Generation rules (all made up for demo purposes, not real-world estimates):
  Households (assigned round-robin to villages):
    tenure            owner 60% / tenant 25% / sharecrop 15%
    acres             owner U(1, 8), tenant/sharecrop U(1, 4), rounded to 0.5
    irrigation        rainfed 55% / well 30% / borewell 15%
    water_hours/week  rainfed 0, well U(2, 12), borewell U(6, 20)
    cattle_count      0-4 (weights 15/30/30/15/10 %), goats_count 0-6
    fodder_weeks_left U(1, 12) if cattle > 0 else 0
    cash_buffer_band  from score = acres/4 + irrigated bonus + off-farm bonus + N(0, 0.5):
                      < 0.8 low, < 1.6 med, else high
    loan_due_month    40% none, else a random month
    consent           always true
  Demo households (fixed, appended after the random ones; own RNG stream, no past outcomes, so the
    25 random households, their outcomes and the calibration stats are unchanged):
    HH-DEMO-OK     well water, 10 h/week, med cash
    HH-DEMO-FLOOR  rainfed, 0 h/week, low cash (water short in a deficit year -> safety floor)
    HH-DEMO-THIN   profile under 60% complete (F1 rule caps confidence at LOW)
    HH-DEMO-LIVESTOCK  2 cows, 3 weeks fodder, low cash, rainfed, no off-farm income (F4)
    HH-DEMO-ONECOW     1 cow, 1 week fodder, low cash (F4: KEEP_WITH_SUPPORT option set)
    HH-DEMO-NOCATTLE   0 cattle (F4 not applicable, no LLM call)
  Plots: one per household, uniform-area random point within 3 km of the village centre.
  Outcomes: 3 rabi seasons. Each village-season draws a hidden rain condition
    (poor 35% / normal 45% / good 20%; not stored). The household picks one of the F3 crop mixes.
    true_risk = water_need(mix) - water_supply + rain_penalty + N(0, 0.4)
      water_need: chana 2.0, chana + safflower 1.5, jowar + fodder 1.0
      water_supply: rainfed 1.0 (residual moisture); well/borewell 1.0 + hours/6, max 3.0
      rain_penalty: poor +1.0, normal 0, good -0.7
    actual_result: P(bad) = sigmoid(1.5 * true_risk); bad is split evenly into loss/partial.
    predicted_signal/confidence come from a noisy estimate of true_risk. The noise is larger for
    rainfed households (sd 1.6 vs 0.4), so HIGH calls there are right less often (checked at
    n=500: about 66% vs 89%, below the 70% threshold). This gives F7 a downgrade to demonstrate.
    distress_sale: P = sigmoid(-2 + 1.5*loss + 1.0*(fodder < 4) + 1.0*(cash low)), only if cattle > 0;
    cattle_sold = 1 if distress_sale else 0.
"""
import argparse
import math

import numpy as np
import pandas as pd

from common import DATA, read_villages, write_csv
from features.f1_interview import build_profile_text

SEASONS = ["rabi-2023-24", "rabi-2024-25", "rabi-2025-26"]
CROP_MIXES = {"Chana only": 2.0, "Chana + safflower": 1.5, "Jowar + fodder": 1.0}  # relative water need
RAIN_PENALTY = {"poor": 1.0, "normal": 0.0, "good": -0.7}
KHARIF_CROPS = ["soybean", "soybean + tur", "cotton", "tur"]
OFF_FARM = ["none", "labour", "MGNREGA", "dairy", "migration"]
RAINFED_RESIDUAL_SUPPLY = 1.0  # rabi on residual soil moisture
ESTIMATE_NOISE_SD = {"rainfed": 1.6, "well": 0.4, "borewell": 0.4}


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def make_households(villages: pd.DataFrame, n: int, rng: np.random.Generator) -> list[dict]:
    rows = []
    for i in range(n):
        village_id = int(villages.iloc[i % len(villages)]["village_id"])
        tenure = rng.choice(["owner", "tenant", "sharecrop"], p=[0.6, 0.25, 0.15])
        acres = round(rng.uniform(1, 8 if tenure == "owner" else 4) * 2) / 2
        irrigation = rng.choice(["rainfed", "well", "borewell"], p=[0.55, 0.3, 0.15])
        hours = {"rainfed": 0, "well": int(rng.uniform(2, 12)), "borewell": int(rng.uniform(6, 20))}[irrigation]
        cattle = int(rng.choice(5, p=[0.15, 0.3, 0.3, 0.15, 0.1]))
        off_farm = str(rng.choice(OFF_FARM))
        score = acres / 4 + (0.5 if irrigation != "rainfed" else 0) + (0.4 if off_farm != "none" else 0)
        score += rng.normal(0, 0.5)
        hh = {
            "hh_id": f"SYN-{i + 1:03d}",
            "village_id": village_id,
            "tenure": str(tenure),
            "acres": acres,
            "irrigation": str(irrigation),
            "water_hours_per_week": hours,
            "crops_last_season": str(rng.choice(KHARIF_CROPS)),
            "cattle_count": cattle,
            "goats_count": int(rng.integers(0, 7)),
            "fodder_weeks_left": int(rng.integers(1, 13)) if cattle > 0 else 0,
            "off_farm_income": off_farm,
            "loan_due_month": None if rng.random() < 0.4 else int(rng.integers(1, 13)),
            "cash_buffer_band": "low" if score < 0.8 else "med" if score < 1.6 else "high",
            "primary_cultivator_gender": str(rng.choice(["man", "woman", "both"], p=[0.6, 0.2, 0.2])),
            "consent": True,
            "is_synthetic": True,
        }
        hh["profile_text_norm"] = build_profile_text(hh)
        rows.append(hh)
    return rows


def make_demo_households(villages: pd.DataFrame) -> list[dict]:
    village_id = int(villages.iloc[0]["village_id"])
    base = {"village_id": village_id, "consent": True, "is_synthetic": True, "goats_count": 0,
            "loan_due_month": None, "primary_cultivator_gender": "both"}
    rows = [
        {**base, "hh_id": "HH-DEMO-OK", "tenure": "owner", "acres": 3.0, "irrigation": "well",
         "water_hours_per_week": 10, "crops_last_season": "soybean + tur", "cattle_count": 2,
         "fodder_weeks_left": 6, "off_farm_income": "dairy", "cash_buffer_band": "med"},
        {**base, "hh_id": "HH-DEMO-FLOOR", "tenure": "tenant", "acres": 2.0, "irrigation": "rainfed",
         "water_hours_per_week": 0, "crops_last_season": "soybean", "cattle_count": 3,
         "fodder_weeks_left": 2, "off_farm_income": "labour", "loan_due_month": 12, "cash_buffer_band": "low"},
        {**base, "hh_id": "HH-DEMO-THIN", "tenure": "tenant", "acres": 1.5, "irrigation": "rainfed",
         "water_hours_per_week": None, "crops_last_season": "soybean", "cattle_count": None, "goats_count": None,
         "fodder_weeks_left": None, "off_farm_income": None, "cash_buffer_band": None,
         "primary_cultivator_gender": None},
        # P4 (F4) seeds, appended so earlier demo rows and their plots are unchanged
        {**base, "hh_id": "HH-DEMO-LIVESTOCK", "tenure": "owner", "acres": 2.0, "irrigation": "rainfed",
         "water_hours_per_week": 0, "crops_last_season": "soybean", "cattle_count": 2, "goats_count": 0,
         "fodder_weeks_left": 3, "off_farm_income": "none", "cash_buffer_band": "low"},
        {**base, "hh_id": "HH-DEMO-ONECOW", "tenure": "tenant", "acres": 1.5, "irrigation": "rainfed",
         "water_hours_per_week": 0, "crops_last_season": "soybean", "cattle_count": 1, "goats_count": 2,
         "fodder_weeks_left": 1, "off_farm_income": "labour", "cash_buffer_band": "low"},
        {**base, "hh_id": "HH-DEMO-NOCATTLE", "tenure": "owner", "acres": 3.0, "irrigation": "well",
         "water_hours_per_week": 6, "crops_last_season": "soybean + tur", "cattle_count": 0, "goats_count": 0,
         "fodder_weeks_left": None, "off_farm_income": "labour", "cash_buffer_band": "med"},
    ]
    for hh in rows:
        hh["profile_text_norm"] = build_profile_text(hh)
    return rows


def make_plots(households: list[dict], villages: pd.DataFrame, rng: np.random.Generator) -> list[dict]:
    centres = villages.set_index("village_id")[["lat", "lon"]]
    rows = []
    for hh in households:
        lat0, lon0 = centres.loc[hh["village_id"]]
        dist_m = 3000 * math.sqrt(rng.random())
        bearing = rng.uniform(0, 2 * math.pi)
        lat = lat0 + dist_m * math.cos(bearing) / 111_320
        lon = lon0 + dist_m * math.sin(bearing) / (111_320 * math.cos(math.radians(lat0)))
        rows.append({
            "plot_id": f"PLOT-{hh['hh_id']}",
            "hh_id": hh["hh_id"],
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "is_synthetic": True,
        })
    return rows


def water_supply(hh: dict) -> float:
    if hh["irrigation"] == "rainfed":
        return RAINFED_RESIDUAL_SUPPLY
    return min(RAINFED_RESIDUAL_SUPPLY + hh["water_hours_per_week"] / 6, 3.0)


RAIN_BUCKET = {"poor": ">30", "normal": "15-30", "good": "0-15"}   # assumption: hidden rain draw -> deficit bucket


def make_outcomes(households: list[dict], rng: np.random.Generator) -> list[dict]:
    return make_outcomes_with_rain(households, rng)[0]


def make_outcomes_with_rain(households: list[dict], rng: np.random.Generator) -> tuple[list[dict], dict]:
    """Same random draws as before; also returns the hidden (village, season) -> rain condition."""
    village_ids = sorted({hh["village_id"] for hh in households})
    rain = {
        (v, s): str(rng.choice(list(RAIN_PENALTY), p=[0.35, 0.45, 0.2]))
        for v in village_ids for s in SEASONS
    }
    rows = []
    for hh in households:
        for season in SEASONS:
            mix = str(rng.choice(list(CROP_MIXES)))
            true_risk = (
                CROP_MIXES[mix] - water_supply(hh) + RAIN_PENALTY[rain[(hh["village_id"], season)]]
                + rng.normal(0, 0.4)
            )
            p_bad = sigmoid(1.5 * true_risk)
            u = rng.random()
            actual = "loss" if u < p_bad / 2 else "partial" if u < p_bad else "good"

            est = true_risk + rng.normal(0, ESTIMATE_NOISE_SD[hh["irrigation"]])
            signal = "ACT" if est < -0.5 else "WAIT" if est < 0.5 else "AVOID"
            confidence = "HIGH" if abs(est) > 1.2 else "MED" if abs(est) > 0.6 else "LOW"

            distress = False
            if hh["cattle_count"] > 0:
                p = sigmoid(-2 + 1.5 * (actual == "loss") + 1.0 * (hh["fodder_weeks_left"] < 4)
                            + 1.0 * (hh["cash_buffer_band"] == "low"))
                distress = bool(rng.random() < p)
            rows.append({
                "outcome_id": f"{hh['hh_id']}:{season}",
                "hh_id": hh["hh_id"],
                "season": season,
                "decision": mix,
                "predicted_signal": signal,
                "predicted_confidence": confidence,
                "actual_result": actual,
                "cattle_sold": 1 if distress else 0,
                "distress_sale": distress,
                "is_synthetic": True,
            })
    return rows, rain


def season_rain_rows(rain: dict) -> list[dict]:
    """SYNTHETIC rain context of the synthetic seasons (the same hidden draw that generated their outcomes)."""
    return [{"village_id": v, "season": s, "rain_deficit_bucket": RAIN_BUCKET[c],
             "source": "SYNTHETIC: make_synthetic hidden rain draw", "is_synthetic": True}
            for (v, s), c in sorted(rain.items())]


def calibration_seed(households: list[dict]) -> list[dict]:
    """F7 demo seed (SYNTHETIC, labelled): past HIGH-confidence ACT calls for 'Chana only' on rainfed farms of
    the first village in a >30% deficit year that mostly failed (2 good / 5 loss), so the context
    '>30|rainfed|gram' has a HIGH hit rate below 0.70 with n >= 5. Fixed values, no randomness."""
    first_village = min(h["village_id"] for h in households)
    rainfed = [h for h in households if h["village_id"] == first_village and h["irrigation"] == "rainfed"
               and h["hh_id"].startswith("SYN-")][:7]
    results = ["good", "loss", "loss", "good", "loss", "loss", "loss"]
    return [{"outcome_id": f"SEED-CAL-{i + 1:03d}", "hh_id": h["hh_id"], "season": "rabi-2022-23",
             "decision": "Chana only", "predicted_signal": "ACT", "predicted_confidence": "HIGH",
             "actual_result": r, "cattle_sold": 0, "distress_sale": False, "request_id": None,
             "rain_deficit_bucket": ">30", "crop_key": "gram",
             "note": "calibration demo seed (SYNTHETIC)", "is_synthetic": True}
            for i, (h, r) in enumerate(zip(rainfed, results))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    villages = read_villages()
    households = make_households(villages, args.n, rng)
    plots = make_plots(households, villages, rng)
    outcomes, rain = make_outcomes_with_rain(households, rng)
    outcomes += calibration_seed(households)
    demo = make_demo_households(villages)
    households += demo
    plots += make_plots(demo, villages, np.random.default_rng(args.seed + 1))

    label = [f"SYNTHETIC: generated by scripts/make_synthetic.py --n {args.n} --seed {args.seed}",
             "Rules documented in the script header. Not real household data."]
    write_csv(pd.DataFrame(households), DATA / "households.csv", label)
    write_csv(pd.DataFrame(plots), DATA / "plots.csv", label)
    write_csv(pd.DataFrame(outcomes), DATA / "outcomes.csv", label)
    write_csv(pd.DataFrame(season_rain_rows(rain)), DATA / "season_rain.csv",
              label + ["rain_deficit_bucket from the hidden rain draw: poor >30, normal 15-30, good 0-15 (assumption)"])
    print(f"wrote {len(households)} households ({len(demo)} demo), {len(plots)} plots, "
          f"{len(outcomes)} outcomes (SYNTHETIC)")


if __name__ == "__main__":
    main()
