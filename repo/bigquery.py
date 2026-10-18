"""BigQuery implementation of repo.base (DATA_BACKEND=bigquery). Same table and column names as Postgres.

Writes are single inserts (insert_rows_json, streaming) - never insert-then-update. Reference tables are
replaced with a load job (replace_table, WRITE_TRUNCATE). Reads are parameterised queries.
Table schemas come from db/bq_schema.json (generated from db/schema.sql); calibration_v is a view built
from db/bq_calibration_v.sql (same SQL as Postgres). The dataset itself must already exist.
"""
import json
import math
import os
from datetime import date, datetime
from pathlib import Path

from repo.base import TABLES, Repo, check_columns

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_JSON = ROOT / "db" / "bq_schema.json"
VIEW_SQL = ROOT / "db" / "bq_calibration_v.sql"
VIEWS = {"calibration_v"}


def _jsonable(v):
    """Python values -> what insert_rows_json / load_table_from_json accept."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (dict,)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "item"):          # numpy scalars
        return v.item()
    return v


def to_rows(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


def _plain(row) -> dict:
    """BigQuery Row -> dict with the same Python types the Postgres repo returns."""
    return {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in dict(row.items()).items()}


class BigQueryRepo(Repo):
    def __init__(self, project: str | None = None, dataset: str | None = None, location: str | None = None,
                 client=None):
        from google.cloud import bigquery  # imported here so local Postgres runs never need it

        self.bq = bigquery
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.dataset = dataset or os.getenv("BQ_DATASET", "seasonsaathi")
        self.location = location or os.getenv("BQ_LOCATION") or None
        self.client = client or bigquery.Client(project=self.project, location=self.location)

    # ---------- helpers ----------
    def ref(self, table: str) -> str:
        return f"{self.project}.{self.dataset}.{table}"

    def schema(self, table: str) -> list:
        fields = json.loads(SCHEMA_JSON.read_text())[table]
        return [self.bq.SchemaField(f["name"], f["type"], mode=f["mode"]) for f in fields]

    def _query(self, sql: str, params: list | None = None) -> list[dict]:
        cfg = self.bq.QueryJobConfig(query_parameters=params or [])
        return [_plain(r) for r in self.client.query(sql, job_config=cfg, location=self.location).result()]

    # ---------- Repo interface ----------
    def init_schema(self) -> None:
        """Create missing tables from db/bq_schema.json, add new columns to existing ones, (re)create the view."""
        from google.api_core.exceptions import NotFound

        try:
            self.client.get_dataset(f"{self.project}.{self.dataset}")
        except NotFound:
            raise SystemExit(f"BigQuery dataset {self.project}:{self.dataset} does not exist. Create it once with:\n"
                             f"  bq --location={self.location or 'REGION'} mk --dataset {self.project}:{self.dataset}") from None
        for table in json.loads(SCHEMA_JSON.read_text()):
            wanted = self.schema(table)
            try:
                existing = self.client.get_table(self.ref(table))
            except NotFound:
                self.client.create_table(self.bq.Table(self.ref(table), schema=wanted))
                continue
            have = {f.name for f in existing.schema}
            extra = [f for f in wanted if f.name not in have]
            if extra:   # additive schema change only (BigQuery allows adding NULLABLE/REPEATED columns)
                existing.schema = list(existing.schema) + [
                    self.bq.SchemaField(f.name, f.field_type, mode="NULLABLE" if f.mode == "REQUIRED" else f.mode)
                    for f in extra]
                self.client.update_table(existing, ["schema"])
        view_sql = VIEW_SQL.read_text().replace("${DATASET}", f"{self.project}.{self.dataset}")
        self.client.query(view_sql, location=self.location).result()

    def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        if table in VIEWS:
            raise ValueError(f"{table} is a view")
        check_columns(table, rows[0].keys())
        errors = self.client.insert_rows_json(self.ref(table), to_rows(rows))
        if errors:
            raise RuntimeError(f"BigQuery insert into {table} failed: {errors[:3]}")
        return len(rows)

    def upsert(self, table: str, rows: list[dict]) -> int:
        """BigQuery has no cheap upsert; app code uses insert(). Loading uses replace_table()."""
        return self.insert(table, rows)

    def replace_table(self, table: str, rows: list[dict]) -> int:
        """Reference data: one load job with WRITE_TRUNCATE (atomic replace)."""
        check_columns(table, rows[0].keys() if rows else [])
        cfg = self.bq.LoadJobConfig(schema=self.schema(table), write_disposition="WRITE_TRUNCATE")
        self.client.load_table_from_json(to_rows(rows), self.ref(table), job_config=cfg,
                                         location=self.location).result()
        return len(rows)

    def table_exists(self, table: str) -> bool:
        from google.api_core.exceptions import NotFound
        try:
            self.client.get_table(self.ref(table))
            return True
        except NotFound:
            return False

    def count(self, table: str) -> int:
        check_columns(table, [])
        return self._query(f"SELECT COUNT(*) AS n FROM `{self.ref(table)}`")[0]["n"]

    def fetch_all(self, table: str) -> list[dict]:
        check_columns(table, [])
        key = ", ".join(f"`{k}`" for k in TABLES[table]["key"])
        return self._query(f"SELECT * FROM `{self.ref(table)}` ORDER BY {key}")

    def fetch_where(self, table: str, **equals) -> list[dict]:
        check_columns(table, equals)
        conds, params = [], []
        for i, (col, val) in enumerate(equals.items()):
            conds.append(f"LOWER(CAST(`{col}` AS STRING)) = LOWER(@p{i})")
            params.append(self.bq.ScalarQueryParameter(f"p{i}", "STRING", str(val)))
        where = " AND ".join(conds) or "TRUE"
        return self._query(f"SELECT * FROM `{self.ref(table)}` WHERE {where}", params)

    def set_embedding(self, hh_id: str, embedding: list[float], embed_model: str | None = None) -> None:
        raise NotImplementedError("BigQuery: no insert-then-update. Embeddings are written with the household row "
                                  "(scripts/load_data.py --backend bigquery, or POST /api/households).")

    def truncate_all(self) -> None:
        raise NotImplementedError("truncate_all is for local test databases only")
