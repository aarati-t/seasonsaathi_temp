"""rain_series / ndvi_series carry source (CHIRPS / Sentinel-2 / SYNTHETIC); facts show it."""
import pytest

from load_data import load_all
from tests.fakes import demo_repo
from tools.ndvi import get_crop_health
from tools.rain import get_rain_status


def test_rain_facts_show_chirps_source():
    facts = get_rain_status(1, demo_repo())
    assert all(f["data_source"] == "CHIRPS" and not f["is_synthetic"] for f in facts)


def test_synthetic_rain_rows_make_facts_synthetic():
    repo = demo_repo()
    repo.tables["rain_series"][0]["source"] = "SYNTHETIC"
    facts = get_rain_status(1, repo)
    assert all(f["is_synthetic"] and "SYNTHETIC" in f["data_source"] and f["source"].startswith("SYNTHETIC")
               for f in facts)


def test_ndvi_facts_show_sentinel_source():
    repo = demo_repo()
    repo.tables["plots"] = [{"plot_id": "P1", "hh_id": "T-1", "lat": 0.0, "lon": 0.0, "is_synthetic": True}]
    repo.tables["ndvi_series"] = [{"plot_id": "P1", "date": "2026-10-01", "ndvi": 0.5, "ndvi_normal": 0.6,
                                   "source": "Sentinel-2"}]
    facts = get_crop_health("P1", repo)
    assert all(f["data_source"] == "Sentinel-2" for f in facts)
    assert facts[0]["is_synthetic"]  # plot location is synthetic even though NDVI is real


class NoDbRepo:
    def init_schema(self):
        pass

    def upsert(self, table, rows):
        return len(rows)


def test_loader_rejects_rain_without_source(tmp_path):
    (tmp_path / "rain_series.csv").write_text("village_id,date,rain_mm,normal_mm,cum_deficit_pct\n1,2026-06-01,1,2,3\n")
    with pytest.raises(SystemExit, match="needs source"):
        load_all(NoDbRepo(), tmp_path, tables=["rain_series"])
