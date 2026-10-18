"""make_synthetic seeds 3 fixed demo households without changing the random ones; peers need history."""
import numpy as np
import pandas as pd

from features.f1_interview import profile_completeness
from make_synthetic import make_demo_households, make_households
from tests.fakes import demo_repo
from tools.peers import similar

VILLAGES = pd.DataFrame([{"village_id": 1, "name": "V", "taluka": "T", "district": "D", "lat": 0.0, "lon": 0.0}])


def test_demo_households_match_their_roles():
    demo = {h["hh_id"]: h for h in make_demo_households(VILLAGES)}
    assert set(demo) == {"HH-DEMO-OK", "HH-DEMO-FLOOR", "HH-DEMO-THIN", "HH-DEMO-LIVESTOCK", "HH-DEMO-ONECOW",
                         "HH-DEMO-NOCATTLE"}
    live = demo["HH-DEMO-LIVESTOCK"]
    assert (live["cattle_count"], live["fodder_weeks_left"], live["cash_buffer_band"], live["irrigation"],
            live["off_farm_income"]) == (2, 3, "low", "rainfed", "none")
    assert (demo["HH-DEMO-ONECOW"]["cattle_count"], demo["HH-DEMO-ONECOW"]["fodder_weeks_left"]) == (1, 1)
    assert demo["HH-DEMO-NOCATTLE"]["cattle_count"] == 0
    assert (demo["HH-DEMO-OK"]["irrigation"], demo["HH-DEMO-OK"]["cash_buffer_band"]) == ("well", "med")
    assert (demo["HH-DEMO-FLOOR"]["irrigation"], demo["HH-DEMO-FLOOR"]["water_hours_per_week"],
            demo["HH-DEMO-FLOOR"]["cash_buffer_band"]) == ("rainfed", 0, "low")
    assert profile_completeness(demo["HH-DEMO-THIN"]) < 60 <= profile_completeness(demo["HH-DEMO-OK"])
    assert all(h["is_synthetic"] and h["consent"] for h in demo.values())


def test_random_households_do_not_depend_on_demo_rows():
    a = make_households(VILLAGES, 5, np.random.default_rng(42))
    b = make_households(VILLAGES, 5, np.random.default_rng(42))
    assert a == b


def test_peers_exclude_households_without_history():
    repo = demo_repo()  # outcomes exist only for T-2
    assert [p for _, p in similar("T-1", repo, 3)] == ["T-2"]
