"""PostgreSQL implementation of repo.base."""
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from repo.base import TABLES, Repo, check_columns

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# Child tables first, so truncation respects foreign keys.
TRUNCATE_ORDER = [
    "decision_log", "alerts", "feedback", "term_review", "outcomes", "season_rain", "crop_mix_keys", "ndvi_series", "plots", "prices", "price_summary", "livestock_prices",
    "soil_moisture", "rain_series", "households", "villages",
]


class PostgresRepo(Repo):
    def __init__(self, url: str):
        self.url = url

    def _connect(self):
        return psycopg.connect(self.url, row_factory=dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL.read_text())

    def upsert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = list(rows[0].keys())
        check_columns(table, cols)
        key = TABLES[table]["key"]
        missing_key = set(key) - set(cols)
        if missing_key:
            raise ValueError(f"{table} rows are missing key columns {sorted(missing_key)}")
        non_key = [c for c in cols if c not in key]
        conflict = (
            sql.SQL("DO UPDATE SET ")
            + sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in non_key
            )
            if non_key
            else sql.SQL("DO NOTHING")
        )
        query = sql.SQL("INSERT INTO {t} ({cols}) VALUES ({vals}) ON CONFLICT ({key}) {conflict}").format(
            t=sql.Identifier(table),
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
            key=sql.SQL(", ").join(map(sql.Identifier, key)),
            conflict=conflict,
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(query, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = list(rows[0].keys())
        check_columns(table, cols)
        query = sql.SQL("INSERT INTO {t} ({cols}) VALUES ({vals})").format(
            t=sql.Identifier(table), cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(query, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def count(self, table: str) -> int:
        check_columns(table, [])
        with self._connect() as conn:
            q = sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(table))
            return conn.execute(q).fetchone()["n"]

    def fetch_all(self, table: str) -> list[dict]:
        check_columns(table, [])
        key = TABLES[table]["key"]
        q = sql.SQL("SELECT * FROM {t} ORDER BY {k}").format(
            t=sql.Identifier(table), k=sql.SQL(", ").join(map(sql.Identifier, key))
        )
        with self._connect() as conn:
            return conn.execute(q).fetchall()

    def fetch_where(self, table: str, **equals) -> list[dict]:
        check_columns(table, equals)
        conds = [
            sql.SQL("lower({c}::text) = lower(%s)").format(c=sql.Identifier(c)) for c in equals
        ] or [sql.SQL("TRUE")]
        q = sql.SQL("SELECT * FROM {t} WHERE {w}").format(t=sql.Identifier(table), w=sql.SQL(" AND ").join(conds))
        with self._connect() as conn:
            return conn.execute(q, [str(v) for v in equals.values()]).fetchall()

    def set_embedding(self, hh_id: str, embedding: list[float], embed_model: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE households SET profile_embedding = %s, embed_model = %s WHERE hh_id = %s",
                         (embedding, embed_model, hh_id))

    def truncate_all(self) -> None:
        with self._connect() as conn:
            conn.execute(
                sql.SQL("TRUNCATE {} CASCADE").format(
                    sql.SQL(", ").join(map(sql.Identifier, TRUNCATE_ORDER))
                )
            )
