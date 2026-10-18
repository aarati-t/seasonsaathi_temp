"""Shared helpers for data scripts: CSV with '#' header comments, village loading."""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_csv(path: Path) -> pd.DataFrame:
    """Read a data CSV: skips '#' comment lines, strips spaces around headers and values."""
    df = pd.read_csv(path, comment="#", skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df


def write_csv(df: pd.DataFrame, path: Path, comments: list[str]) -> None:
    """Write a CSV whose first lines are '# ...' comments (dataset, date range, SYNTHETIC label)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        for line in comments:
            f.write(f"# {line}\n")
        df.to_csv(f, index=False)


def read_villages(path: Path = DATA / "villages.csv") -> pd.DataFrame:
    df = read_csv(path)
    required = ["village_id", "name", "taluka", "district", "lat", "lon"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"{path}: missing columns {missing}")
    if df[["lat", "lon"]].isna().any().any():
        raise SystemExit(f"{path}: every village needs lat and lon entered by hand")
    return df


def require_household(hh_id: str) -> None:
    """Exit with a readable message (no traceback) if the DB is unreachable or hh_id does not exist."""
    import difflib

    import psycopg

    from repo.base import get_repo

    try:
        ids = sorted(h["hh_id"] for h in get_repo().fetch_all("households"))
    except psycopg.OperationalError as exc:
        first = str(exc).strip().splitlines()[0]
        raise SystemExit(f"Cannot reach the database: {first}\n"
                         "Check that Postgres is running and DATABASE_URL in .env is right.") from None
    if hh_id in ids:
        return
    by_upper = {i.upper(): i for i in ids}
    close = [by_upper[m] for m in difflib.get_close_matches(hh_id.upper(), list(by_upper), n=3, cutoff=0.5)]
    demo = [i for i in ids if i.startswith("HH-DEMO-")]
    others = [i for i in ids if i not in demo]
    lines = [f"Household '{hh_id}' not found ({len(ids)} households in the database)."]
    if close:
        lines.append(f"Did you mean: {', '.join(close)}?")
    if demo:
        lines.append(f"Demo households: {', '.join(demo)}")
    if others:
        lines.append(f"Others: {others[0]} ... {others[-1]} ({len(others)})")
    if not ids:
        lines.append("No households loaded: run scripts/load_data.py")
    raise SystemExit("\n".join(lines))
