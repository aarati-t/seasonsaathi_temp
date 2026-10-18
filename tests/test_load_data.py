"""load_data must be idempotent: loading the same files twice gives identical tables."""
import os

import numpy as np
import pandas as pd
import psycopg
import pytest

from load_data import load_all
from make_synthetic import make_households, make_outcomes, make_plots
from repo.postgres import PostgresRepo

TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql:///seasonsaathi_test")
TABLES = ["villages", "households", "plots", "rain_series", "prices", "outcomes"]


@pytest.fixture
def repo():
    if not psycopg.conninfo.conninfo_to_dict(TEST_DB).get("dbname", "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point at a database whose name ends in _test")
    try:
        psycopg.connect(TEST_DB).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"test database not reachable ({TEST_DB}): {exc}")
    r = PostgresRepo(TEST_DB)
    r.init_schema()
    r.truncate_all()
    yield r
    r.truncate_all()


@pytest.fixture
def data_dir(tmp_path):
    # Test-fixture village; coordinates are placeholders, not a real location.
    villages = pd.DataFrame([{"village_id": 99, "name": "TestVillage", "taluka": "T", "district": "D",
                              "lat": 18.0, "lon": 76.0}])
    rng = np.random.default_rng(0)
    households = make_households(villages, 4, rng)
    villages.to_csv(tmp_path / "villages.csv", index=False)
    pd.DataFrame(households).to_csv(tmp_path / "households.csv", index=False)
    pd.DataFrame(make_plots(households, villages, rng)).to_csv(tmp_path / "plots.csv", index=False)
    pd.DataFrame(make_outcomes(households, rng)).to_csv(tmp_path / "outcomes.csv", index=False)
    (tmp_path / "rain_series.csv").write_text(
        "# test fixture\nvillage_id,date,rain_mm,normal_mm,cum_deficit_pct,source\n"
        "99,2026-06-01,1.5,2.0,25.0,CHIRPS\n99,2026-06-02,,2.0,,CHIRPS\n"
    )
    (tmp_path / "prices.csv").write_text(
        "commodity,district,market,date,modal_price,source,is_synthetic\n"
        "gram,D,,2026-10-01,100,test,true\ngram,D,M1,2026-10-01,110,test,true\n"
    )
    return tmp_path


def snapshot(repo):
    return {t: repo.fetch_all(t) for t in TABLES}


def test_load_twice_gives_same_tables(repo, data_dir):
    load_all(repo, data_dir)
    first = snapshot(repo)
    assert all(first[t] for t in TABLES), "every fixture table should have rows"

    load_all(repo, data_dir)
    assert snapshot(repo) == first


def test_reload_keeps_embeddings(repo, data_dir):
    load_all(repo, data_dir)
    repo.set_embedding("SYN-001", [0.1, 0.2, 0.3])
    load_all(repo, data_dir)
    hh = {h["hh_id"]: h for h in repo.fetch_all("households")}
    assert hh["SYN-001"]["profile_embedding"] == pytest.approx([0.1, 0.2, 0.3])
