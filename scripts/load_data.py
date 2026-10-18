"""Load data/*.csv into the configured backend via repo.base (postgres first).

Usage: python scripts/load_data.py [--backend postgres|bigquery] [--data-dir data] [--tables T1 T2 ...]
  postgres: idempotent upserts.
  bigquery: reference tables WRITE_TRUNCATE (one load job each); decision_log / feedback / term_review / alerts are
            never touched; outcomes are skipped if real outcomes exist; households come from Postgres (with
            embeddings) when reachable and are skipped if BigQuery holds app-saved households not in the source.
Idempotent: tables are created if missing and rows are upserted on their keys, so re-running
gives the same tables. Embeddings written by embed_profiles.py are kept (that column is not in the CSVs).
"""
import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA, read_csv
from repo.base import TABLES, Repo, get_repo

# (table, file) in foreign-key order. Missing files are skipped with a notice.
FILES = [
    ("villages", "villages.csv"),
    ("households", "households.csv"),
    ("households", "survey_households.csv"),
    ("plots", "plots.csv"),
    ("rain_series", "rain_series.csv"),
    ("ndvi_series", "ndvi_series.csv"),
    ("soil_moisture", "soil_moisture.csv"),
    ("soil_props", "soil_props.csv"),
    ("crop_water_need", "crop_water_need.csv"),
    ("prices", "prices.csv"),
    ("price_summary", "price_summary.csv"),
    ("livestock_prices", "livestock_prices.csv"),
    ("season_rain", "season_rain.csv"),
    ("outcomes", "outcomes.csv"),
]
MUST_HAVE_SOURCE = {"rain_series", "ndvi_series"}  # CHIRPS / Sentinel-2 / SYNTHETIC
MUST_HAVE_SYNTHETIC_FLAG = {"households", "plots", "prices", "price_summary", "livestock_prices", "outcomes", "season_rain",
                           "soil_props", "crop_water_need"}


def to_python(v):
    """pandas/numpy scalars -> plain Python; NaN -> None."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    return v.item() if isinstance(v, np.generic) else v


def frame_to_rows(table: str, df: pd.DataFrame) -> list[dict]:
    cols = [c for c in TABLES[table]["cols"] if c in df.columns]
    df = df[cols].astype(object).where(df[cols].notna(), None)
    return [{c: to_python(v) for c, v in r.items()} for r in df.to_dict("records")]


def crop_mix_rows() -> list[dict]:
    """Crop-mix decision -> canonical crop key, from features.f3_cropmix (config, not data)."""
    from features.f3_cropmix import CROP_KEYS
    return [{"decision": d, "crop_key": k} for d, k in CROP_KEYS.items()]


def load_all(repo: Repo, data_dir: Path = DATA, tables: list[str] | None = None) -> dict[str, int]:
    repo.init_schema()
    loaded: dict[str, int] = {}
    if not tables or "crop_mix_keys" in tables:
        loaded["crop_mix_keys"] = repo.upsert("crop_mix_keys", crop_mix_rows())
    for table, name in FILES:
        if tables and table not in tables:
            continue
        path = data_dir / name
        if not path.exists():
            print(f"  skip {name} (not found)")
            continue
        try:
            df = read_csv(path)
        except pd.errors.EmptyDataError:
            print(f"  skip {name} (empty)")
            continue
        if table in MUST_HAVE_SYNTHETIC_FLAG and "is_synthetic" not in df.columns:
            raise SystemExit(f"{name}: every {table} row needs is_synthetic")
        if table in MUST_HAVE_SOURCE and ("source" not in df.columns or df["source"].isna().any()):
            raise SystemExit(f"{name}: every {table} row needs source (CHIRPS / Sentinel-2 / SYNTHETIC); "
                             "re-run scripts/ee_export.py")
        if table == "outcomes" and "outcome_id" not in df.columns:   # pre-P4 CSVs
            df["outcome_id"] = df["hh_id"].astype(str) + ":" + df["season"].astype(str)
        n = repo.upsert(table, frame_to_rows(table, df))
        loaded[table] = loaded.get(table, 0) + n
        print(f"  {name:28s} -> {table:14s} {n} rows")
    return loaded


APP_TABLES = {"decision_log", "feedback", "term_review", "alerts", "impact_cache", "impact_runs", "verifier_benchmark"}   # written by the app; never loaded from CSV


def _csv_rows(table: str, data_dir: Path) -> list[dict] | None:
    """All CSV rows for a table (households has two files), validated like the Postgres path."""
    rows, found = [], False
    for t, name in FILES:
        if t != table or not (data_dir / name).exists():
            continue
        try:
            df = read_csv(data_dir / name)
        except pd.errors.EmptyDataError:
            continue
        found = True
        if table in MUST_HAVE_SYNTHETIC_FLAG and "is_synthetic" not in df.columns:
            raise SystemExit(f"{name}: every {table} row needs is_synthetic")
        if table in MUST_HAVE_SOURCE and ("source" not in df.columns or df["source"].isna().any()):
            raise SystemExit(f"{name}: every {table} row needs source; re-run scripts/ee_export.py")
        if table == "outcomes" and "outcome_id" not in df.columns:
            df["outcome_id"] = df["hh_id"].astype(str) + ":" + df["season"].astype(str)
        rows += frame_to_rows(table, df)
    return rows if found else None


def households_for_bigquery(data_dir: Path) -> tuple[list[dict], str]:
    """Households with embeddings in the same row: from Postgres when reachable (includes HH-NEW-* saved by the
    app), else from the CSVs with embeddings computed now by EMBED_MODEL."""
    try:
        from repo.postgres import PostgresRepo
        pg = PostgresRepo(os.getenv("PG_SOURCE_URL", "postgresql:///seasonsaathi"))
        rows = pg.fetch_all("households")
        if rows and all(r["profile_embedding"] for r in rows if r["profile_text_norm"]):
            return [{c: r.get(c) for c in TABLES["households"]["cols"]} for r in rows], "Postgres (with embeddings)"
    except Exception as exc:  # noqa: BLE001 - Postgres is optional here
        print(f"  Postgres not used for households ({type(exc).__name__}); embedding CSV households now")
    from core import llm
    rows = _csv_rows("households", data_dir) or []
    todo = [r for r in rows if r.get("profile_text_norm")]
    for i in range(0, len(todo), 20):
        vecs = llm.embed([r["profile_text_norm"] for r in todo[i:i + 20]])
        for r, v in zip(todo[i:i + 20], vecs, strict=True):
            r["profile_embedding"], r["embed_model"] = v, llm.require_env("EMBED_MODEL")
    return rows, "CSV + EMBED_MODEL"


def load_bigquery(repo, data_dir: Path = DATA, tables: list[str] | None = None) -> dict[str, str]:
    """Reference tables: WRITE_TRUNCATE. App tables: untouched. outcomes / households: kept if the app wrote to them."""
    repo.init_schema()
    report = {}
    order = ["crop_mix_keys"] + list(dict.fromkeys(t for t, _ in FILES))
    for table in order:
        if tables and table not in tables:
            continue
        if table == "crop_mix_keys":
            rows, src = crop_mix_rows(), "config"
        elif table == "households":
            rows, src = households_for_bigquery(data_dir)
            existing = {r["hh_id"] for r in repo.fetch_all("households")} if repo.table_exists("households") else set()
            app_only = existing - {r["hh_id"] for r in rows}
            if app_only:
                report[table] = f"SKIPPED: BigQuery has {len(app_only)} household(s) not in the source (saved by the app), e.g. {sorted(app_only)[:3]}"
                print(f"  households -> {report[table]}")
                continue
        else:
            rows, src = _csv_rows(table, data_dir), "CSV"
            if rows is None:
                report[table] = "skipped (no CSV)"
                print(f"  {table:16s} skipped (no CSV)")
                continue
        if table == "outcomes" and repo.table_exists("outcomes"):
            real = [r for r in repo.fetch_where("outcomes", is_synthetic="false")]
            if real:
                report[table] = f"SKIPPED: {len(real)} real outcome(s) recorded by the app; not replaced"
                print(f"  outcomes -> {report[table]}")
                continue
        n = repo.replace_table(table, rows)
        report[table] = f"{n} rows (WRITE_TRUNCATE from {src})"
        print(f"  {table:16s} {report[table]}")
    for t in sorted(APP_TABLES):
        print(f"  {t:16s} untouched (app-written)")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--backend", choices=["postgres", "bigquery"], help="overrides DATA_BACKEND")
    ap.add_argument("--tables", nargs="+", choices=sorted({t for t, _ in FILES} | {"crop_mix_keys"}),
                    help="load only these tables")
    args = ap.parse_args()
    if args.backend:
        os.environ["DATA_BACKEND"] = args.backend
    repo = get_repo()
    if os.getenv("DATA_BACKEND") == "bigquery":
        load_bigquery(repo, args.data_dir, args.tables)
    else:
        load_all(repo, args.data_dir, args.tables)
    for table in args.tables or dict.fromkeys(t for t, _ in FILES):
        print(f"  {table:14s} total {repo.count(table)}")


if __name__ == "__main__":
    main()
