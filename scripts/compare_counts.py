"""Row counts per table: Postgres (local) vs BigQuery. No writes.

Usage: python scripts/compare_counts.py
"""
from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from repo.base import TABLES

APP_TABLES = {"decision_log", "feedback", "term_review", "alerts", "impact_cache", "impact_runs", "verifier_benchmark"}
from repo.bigquery import BigQueryRepo
from repo.postgres import PostgresRepo


def main() -> None:
    pg, bq = PostgresRepo("postgresql:///seasonsaathi"), BigQueryRepo()
    print(f"{'table':18s} {'postgres':>9s} {'bigquery':>9s}  match")
    ok = True
    for table in TABLES:
        try:
            a = pg.count(table)
        except Exception as exc:  # noqa: BLE001
            a = f"err:{type(exc).__name__}"
        try:
            b = bq.count(table) if table == "calibration_v" or bq.table_exists(table) else "missing"
        except Exception as exc:  # noqa: BLE001
            b = f"err:{type(exc).__name__}"
        same = a == b
        ok &= same or table in APP_TABLES
        print(f"{table:18s} {str(a):>9s} {str(b):>9s}  {'yes' if same else 'NO'}")
    print(f"\nApp-written tables ({', '.join(sorted(APP_TABLES))}) are expected to differ.")
    print("RESULT:", "PASS" if ok else "FAIL (reference tables differ)")


if __name__ == "__main__":
    main()
