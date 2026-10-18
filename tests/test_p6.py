"""P6 (unit, no GCP): BigQuery repo with a fake client, bq_schema generation, BigQuery load rules,
CSV fallback parity, storage backends. No network calls."""
import json
from datetime import date

import pytest
from google.api_core.exceptions import NotFound

import gen_bq_schema
import load_data
from repo.bigquery import BigQueryRepo, to_rows
from services import storage


class FakeJob:
    def __init__(self, rows=None):
        self.rows = rows or []

    def result(self, timeout=None):
        return self.rows


class FakeBQ:
    """Records calls; get_table says every table is missing."""
    def __init__(self, existing=()):
        self.created, self.inserted, self.loaded, self.queries, self.existing = [], [], [], [], set(existing)

    def get_dataset(self, ref):
        return ref

    def get_table(self, ref):
        if ref.split(".")[-1] not in self.existing:
            raise NotFound(ref)

        class T:
            schema = []
        return T()

    def create_table(self, table):
        self.created.append(table.table_id)

    def update_table(self, table, fields):
        pass

    def query(self, sql, job_config=None, location=None):
        self.queries.append((sql, job_config))
        return FakeJob([])

    def insert_rows_json(self, ref, rows):
        self.inserted.append((ref, rows))
        return []

    def load_table_from_json(self, rows, ref, job_config=None, location=None):
        self.loaded.append((ref, rows, job_config.write_disposition))
        return FakeJob()


def repo(existing=()):
    return BigQueryRepo(project="p", dataset="seasonsaathi", client=FakeBQ(existing))


def test_bq_schema_generated_from_schema_sql_covers_all_tables():
    tables = gen_bq_schema.parse(gen_bq_schema.SQL.read_text())
    assert set(tables) >= {"households", "plots", "rain_series", "ndvi_series", "prices", "price_summary",
                           "livestock_prices", "outcomes", "season_rain", "crop_mix_keys", "decision_log", "alerts",
                           "feedback", "term_review"}
    hh = {c["name"]: c for c in tables["households"]}
    assert hh["profile_embedding"] == {"name": "profile_embedding", "type": "FLOAT64", "mode": "REPEATED"}
    assert hh["embed_model"]["type"] == "STRING" and hh["is_synthetic"]["mode"] == "REQUIRED"
    assert {c["name"] for c in tables["alerts"]} >= {"message_id", "fact_ids", "data_as_of"}
    assert json.loads(gen_bq_schema.OUT.read_text()) == tables          # committed file is up to date


def test_init_schema_creates_tables_and_the_calibration_view():
    r = repo()
    r.init_schema()
    assert "households" in r.client.created and "decision_log" in r.client.created
    view_sql = r.client.queries[-1][0]
    assert "CREATE OR REPLACE VIEW `p.seasonsaathi.calibration_v`" in view_sql and "n_synthetic" in view_sql


def test_insert_is_streaming_insert_with_json_values():
    r = repo()
    r.insert("alerts", [{"alert_id": "A", "plot_id": "P", "created_at": date(2026, 10, 1), "fact_ids": ["x"]}])
    ref, rows = r.client.inserted[0]
    assert ref == "p.seasonsaathi.alerts" and rows == [{"alert_id": "A", "plot_id": "P", "created_at": "2026-10-01",
                                                         "fact_ids": ["x"]}]
    with pytest.raises(ValueError):
        r.insert("calibration_v", [{"context_bucket": "x"}])
    with pytest.raises(NotImplementedError):
        r.set_embedding("H", [0.1])                                       # no insert-then-update


def test_replace_table_is_write_truncate_load_and_fetch_where_is_parameterised():
    r = repo()
    r.replace_table("villages", [{"village_id": 1, "name": "V", "taluka": "T", "district": "D", "lat": 0.0, "lon": 0.0}])
    assert r.client.loaded[0][0] == "p.seasonsaathi.villages" and r.client.loaded[0][2] == "WRITE_TRUNCATE"
    r.fetch_where("rain_series", village_id=1)
    sql, cfg = r.client.queries[-1]
    assert "LOWER(CAST(`village_id` AS STRING)) = LOWER(@p0)" in sql and cfg.query_parameters[0].value == "1"


def test_to_rows_handles_nan_numpy_and_dicts():
    import numpy as np
    assert to_rows([{"a": float("nan"), "b": np.int64(3), "c": {"k": 1}}]) == [{"a": None, "b": 3, "c": '{"k": 1}'}]


class LoadRepo:
    """Fake repo for load_bigquery: tracks replaced tables; can pretend app data exists."""
    def __init__(self, real_outcomes=0, app_households=()):
        self.replaced, self.real, self.app_hh = {}, real_outcomes, set(app_households)

    def init_schema(self):
        pass

    def table_exists(self, table):
        return True

    def fetch_all(self, table):
        return [{"hh_id": h} for h in self.app_hh] if table == "households" else []

    def fetch_where(self, table, **eq):
        return [{"outcome_id": f"R{i}"} for i in range(self.real)] if table == "outcomes" else []

    def replace_table(self, table, rows):
        self.replaced[table] = len(rows)
        return len(rows)


def test_load_bigquery_truncates_reference_tables_and_never_app_tables(monkeypatch):
    monkeypatch.setattr(load_data, "households_for_bigquery", lambda d: ([{"hh_id": "SYN-001"}], "test"))
    r = LoadRepo()
    report = load_data.load_bigquery(r)
    assert {"villages", "rain_series", "ndvi_series", "prices", "livestock_prices", "outcomes", "crop_mix_keys",
            "households"} <= set(r.replaced)
    assert not set(r.replaced) & load_data.APP_TABLES
    assert "WRITE_TRUNCATE" in report["villages"]


def test_load_bigquery_keeps_real_outcomes_and_app_saved_households(monkeypatch):
    monkeypatch.setattr(load_data, "households_for_bigquery", lambda d: ([{"hh_id": "SYN-001"}], "test"))
    r = LoadRepo(real_outcomes=2, app_households={"SYN-001", "HH-NEW-007"})
    report = load_data.load_bigquery(r)
    assert "outcomes" not in r.replaced and "2 real outcome(s)" in report["outcomes"]
    assert "households" not in r.replaced and "HH-NEW-007" in report["households"]


def test_csv_fallback_calibration_matches_the_sql_rule():
    from repo.csvrepo import CsvRepo, hit
    c = CsvRepo()
    rows = {(r["context_bucket"], r["confidence_level"]): r for r in c.fetch_all("calibration_v")}
    assert rows[(">30|rainfed|gram", "HIGH")]["n"] >= 7            # includes the labelled SEED-CAL rows
    assert hit("ACT", "good") and hit("AVOID", "loss") and hit("WAIT", "partial") and not hit("ACT", "loss")
    c.insert("feedback", [{"feedback_id": "F1", "request_id": "R", "rating": "up"}])
    assert c.count("feedback") == 1                                 # in memory only


def test_storage_local_and_gcs(tmp_path, monkeypatch):
    local = storage.LocalStorage(root=tmp_path)
    out = local.save("audio", b"RIFF1234", "audio/wav", "wav")
    assert out["path"].startswith("audio/") and out["path"].endswith(".wav") and local.read(out["uri"]) == b"RIFF1234"
    with pytest.raises(ValueError):
        local.save("other", b"x", "text/plain", "txt")

    class Blob:
        def __init__(self, store, name):
            self.store, self.name = store, name

        def upload_from_string(self, data, content_type=None):
            self.store[self.name] = (data, content_type)

        def download_as_bytes(self):
            return self.store[self.name][0]

    class Bucket:
        def __init__(self):
            self.store = {}

        def blob(self, name):
            return Blob(self.store, name)

    class Client:
        def __init__(self):
            self.b = Bucket()

        def bucket(self, name):
            return self.b

    gcs = storage.GCSStorage(bucket="proj-seasonsaathi", client=Client())
    out = gcs.save("evidence", b"%PDF", "application/pdf", "pdf")
    assert out["uri"].startswith("gs://proj-seasonsaathi/evidence/") and gcs.read(out["uri"]) == b"%PDF"
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="GCS_BUCKET"):
        storage.GCSStorage(client=Client())
