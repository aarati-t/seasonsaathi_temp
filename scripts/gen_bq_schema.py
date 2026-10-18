"""Generate db/bq_schema.json (BigQuery table schemas) from db/schema.sql, so both stay in step.

Usage: python scripts/gen_bq_schema.py
Type map: TEXT->STRING, INTEGER/SMALLINT->INT64, DOUBLE PRECISION/REAL->FLOAT64, BOOLEAN->BOOL, DATE->DATE,
TIMESTAMPTZ->TIMESTAMP, JSONB->STRING (JSON text), REAL[]->FLOAT64 REPEATED. NOT NULL / PRIMARY KEY -> REQUIRED.
Columns added later with ALTER TABLE ... ADD COLUMN are included. Views are not tables (calibration_v is
created from db/bq_calibration_v.sql). Constraints (CHECK, FK, keys) do not exist in BigQuery.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "db" / "schema.sql"
OUT = ROOT / "db" / "bq_schema.json"

TYPES = [("DOUBLE PRECISION", "FLOAT64"), ("TIMESTAMPTZ", "TIMESTAMP"), ("SMALLINT", "INT64"), ("INTEGER", "INT64"),
         ("BOOLEAN", "BOOL"), ("JSONB", "STRING"), ("REAL[]", "FLOAT64"), ("REAL", "FLOAT64"), ("TEXT", "STRING"),
         ("DATE", "DATE")]
SKIP = ("PRIMARY KEY", "UNIQUE", "CHECK", "CONSTRAINT", "FOREIGN KEY", "--", ")")


def bq_field(name: str, spec: str) -> dict:
    up = spec.upper()
    for pg, bq in TYPES:
        if up.startswith(pg):
            mode = "REPEATED" if pg.endswith("[]") else "REQUIRED" if ("NOT NULL" in up or "PRIMARY KEY" in up) else "NULLABLE"
            return {"name": name, "type": bq, "mode": mode}
    raise ValueError(f"unmapped type for {name}: {spec}")


def parse(sql: str) -> dict[str, list[dict]]:
    sql = re.sub(r"DO \$\$.*?\$\$;", "", sql, flags=re.S)          # Postgres-only migration blocks
    tables: dict[str, list[dict]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, flags=re.S):
        cols = []
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(SKIP):
                continue
            name, spec = line.split(None, 1)
            cols.append(bq_field(name, spec.split("--")[0].strip()))
        tables[m.group(1)] = cols
    for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) ([^;\n]+)", sql):
        table, name, spec = m.groups()
        if name not in {c["name"] for c in tables[table]}:
            field = bq_field(name, spec.split("--")[0].strip())
            field["mode"] = "NULLABLE" if field["mode"] == "REQUIRED" else field["mode"]   # added later: may be empty
            tables[table].append(field)
    return tables


def main() -> None:
    tables = parse(SQL.read_text())
    OUT.write_text(json.dumps(tables, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(tables)} tables ({', '.join(tables)})")


if __name__ == "__main__":
    main()
