"""Emergency fallback repo (DATA_BACKEND=csv): read-only data from the CSVs bundled in the image.

Use only if BigQuery blocks the deploy. Reference data comes from data/*.csv; households (with embeddings,
including app-saved HH-NEW-*) from data/bundle/households.csv (scripts/export_bundle.py). calibration_v is
computed in Python with the same rule as the SQL view. App writes (decision_log, feedback, alerts, outcomes,
term_review, saved households) are kept IN MEMORY ONLY and are lost when the instance restarts.
"""
import json
import logging
import math
from pathlib import Path

import pandas as pd

from repo.base import TABLES, Repo, check_columns

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BUNDLE = DATA / "bundle"
FILES = {"villages": ["villages.csv"], "households": ["households.csv", "survey_households.csv"],
         "plots": ["plots.csv"], "rain_series": ["rain_series.csv"], "ndvi_series": ["ndvi_series.csv"],
         "soil_moisture": ["soil_moisture.csv"], "soil_props": ["soil_props.csv"], "crop_water_need": ["crop_water_need.csv"], "prices": ["prices.csv"], "price_summary": ["price_summary.csv"],
         "livestock_prices": ["livestock_prices.csv"], "season_rain": ["season_rain.csv"], "outcomes": ["outcomes.csv"]}
log = logging.getLogger("seasonsaathi.repo")


def _py(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def _read(path: Path, table: str) -> list[dict]:
    df = pd.read_csv(path, comment="#", skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    cols = [c for c in TABLES[table]["cols"] if c in df.columns]
    rows = [{c: _py(v) for c, v in r.items()} for r in df[cols].to_dict("records")]
    if table == "households":
        for r in rows:
            emb = r.get("profile_embedding")
            r["profile_embedding"] = json.loads(emb) if isinstance(emb, str) else None
    if table == "outcomes":
        for r in rows:
            r.setdefault("outcome_id", f"{r['hh_id']}:{r['season']}")
    return rows


def hit(signal: str, result: str) -> bool:
    return (signal == "ACT" and result == "good") or (signal == "AVOID" and result != "good") or \
           (signal == "WAIT" and result == "partial")


class CsvRepo(Repo):
    def __init__(self, data_dir: Path = DATA):
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLES if t != "calibration_v"}
        for table, names in FILES.items():
            if table == "households" and (BUNDLE / "households.csv").exists():
                names = []
                self.tables[table] = _read(BUNDLE / "households.csv", table)
            for name in names:
                if (data_dir / name).exists():
                    try:
                        self.tables[table] += _read(data_dir / name, table)
                    except pd.errors.EmptyDataError:
                        pass
        from features.f3_cropmix import CROP_KEYS
        self.tables["crop_mix_keys"] = [{"decision": d, "crop_key": k} for d, k in CROP_KEYS.items()]
        log.warning("DATA_BACKEND=csv: read-only bundled data; app writes are kept in memory and lost on restart")

    def init_schema(self) -> None:
        return None

    def insert(self, table: str, rows: list[dict]) -> int:
        check_columns(table, rows[0].keys() if rows else [])
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)
        return len(rows)

    def upsert(self, table: str, rows: list[dict]) -> int:
        key = TABLES[table]["key"]
        keep = {tuple(r.get(k) for k in key) for r in rows}
        self.tables[table] = [r for r in self.tables.get(table, []) if tuple(r.get(k) for k in key) not in keep]
        return self.insert(table, rows)

    def count(self, table: str) -> int:
        return len(self.fetch_all(table))

    def fetch_all(self, table: str) -> list[dict]:
        check_columns(table, [])
        if table == "calibration_v":
            return self._calibration()
        return [dict(r) for r in self.tables.get(table, [])]

    def fetch_where(self, table: str, **equals) -> list[dict]:
        check_columns(table, equals)
        return [r for r in self.fetch_all(table)
                if all(str(r.get(k)).lower() == str(v).lower() for k, v in equals.items())]

    def set_embedding(self, hh_id: str, embedding: list[float], embed_model: str | None = None) -> None:
        raise NotImplementedError("csv backend is read-only")

    def truncate_all(self) -> None:
        raise NotImplementedError("csv backend is read-only")

    def _calibration(self) -> list[dict]:
        """Same as the calibration_v SQL view (db/schema.sql)."""
        hh = {h["hh_id"]: h for h in self.tables["households"]}
        rain = {(r["village_id"], r["season"]): r["rain_deficit_bucket"] for r in self.tables["season_rain"]}
        crops = {c["decision"]: c["crop_key"] for c in self.tables["crop_mix_keys"]}
        groups: dict[tuple, list] = {}
        for o in self.tables["outcomes"]:
            if not (o.get("predicted_signal") and o.get("predicted_confidence") and o.get("actual_result")):
                continue
            h = hh.get(o["hh_id"])
            if h is None:
                continue
            bucket = o.get("rain_deficit_bucket") or rain.get((h["village_id"], o["season"])) or "unknown"
            crop = o.get("crop_key") or crops.get(o.get("decision")) or "unknown"
            ctx = f"{bucket}|{h.get('irrigation') or 'unknown'}|{crop}"
            groups.setdefault((ctx, o["predicted_confidence"]), []).append(o)
        return [{"context_bucket": ctx, "confidence_level": lvl,
                 "hit_rate": sum(hit(o["predicted_signal"], o["actual_result"]) for o in rows) / len(rows),
                 "n": len(rows), "n_real": sum(not o["is_synthetic"] for o in rows),
                 "n_synthetic": sum(bool(o["is_synthetic"]) for o in rows)}
                for (ctx, lvl), rows in sorted(groups.items())]
