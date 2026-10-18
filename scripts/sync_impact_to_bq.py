"""Copy the P5b impact tables from local Postgres to BigQuery (append-only: rows whose key already exists in
BigQuery are skipped, nothing is overwritten or deleted). Then check that a replay on BigQuery data finds the cache.

Usage (same env as load_data.py --backend bigquery):
  GOOGLE_CLOUD_PROJECT=$PROJECT BQ_DATASET=seasonsaathi BQ_LOCATION=$REGION python scripts/sync_impact_to_bq.py
  ... python scripts/sync_impact_to_bq.py --check   # also: cache keys on BigQuery data == Postgres data (no writes)
"""
import argparse

from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from repo.base import TABLES
from repo.bigquery import BigQueryRepo
from repo.postgres import PostgresRepo

IMPACT_TABLES = ["impact_cache", "verifier_benchmark", "impact_runs"]


def key_of(table: str, row: dict) -> tuple:
    return tuple(str(row[k]) for k in TABLES[table]["key"])


def sync(pg, bq) -> None:
    bq.init_schema()
    for table in IMPACT_TABLES:
        have = {key_of(table, r) for r in bq.fetch_all(table)}
        rows = [r for r in pg.fetch_all(table) if key_of(table, r) not in have]
        for i in range(0, len(rows), 20):           # small batches: impact_cache rows carry full results
            bq.insert(table, rows[i:i + 20])
        print(f"  {table:20s} copied {len(rows)} (already in BigQuery: {len(have)})")


def check(pg, bq) -> bool:
    """Do F3/F4 fact snapshots computed from BigQuery match Postgres? (else the cloud replay would miss the cache)"""
    from features import impact
    pop = impact.population(impact.ReadCache(pg))
    a, b = impact.ReadCache(pg), impact.ReadCache(bq)
    diff = [(h, f) for h, f in impact.wanted(pop) if impact.fact_snapshot(f, h, a) != impact.fact_snapshot(f, h, b)]
    total = len(impact.wanted(pop))
    print(f"  fact snapshots equal on Postgres and BigQuery: {total - len(diff)}/{total}")
    for h, f in diff[:10]:
        print(f"    differs: {f} {h}")
    return not diff


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="also compare fact snapshots (cache keys) Postgres vs BigQuery")
    args = ap.parse_args()
    pg, bq = PostgresRepo("postgresql:///seasonsaathi"), BigQueryRepo()
    sync(pg, bq)
    if args.check:
        print("RESULT: PASS (cloud replay will find the cache)" if check(pg, bq)
              else "RESULT: FAIL (cloud replay would miss these entries; see above)")


if __name__ == "__main__":
    main()
