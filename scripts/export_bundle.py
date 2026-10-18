"""Export households (with embeddings, incl. app-saved HH-NEW-*) from Postgres to data/bundle/households.csv,
for the emergency DATA_BACKEND=csv fallback. Other tables are read from the existing data/*.csv files.

Usage: python scripts/export_bundle.py
"""
import json

import pandas as pd

from common import DATA
from repo.base import TABLES
from repo.postgres import PostgresRepo


def main() -> None:
    rows = PostgresRepo("postgresql:///seasonsaathi").fetch_all("households")
    for r in rows:
        r["profile_embedding"] = json.dumps(r["profile_embedding"]) if r["profile_embedding"] else None
    out = DATA / "bundle" / "households.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows)[TABLES["households"]["cols"]].to_csv(out, index=False)
    print(f"wrote {out.relative_to(DATA.parent)}: {len(rows)} households "
          f"({sum(1 for r in rows if r['profile_embedding'])} with embeddings)")


if __name__ == "__main__":
    main()
