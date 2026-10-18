"""Google Form survey CSV (SPEC Appendix A) -> data/survey_households.csv (is_synthetic=false).

Usage: python scripts/import_survey.py path/to/form_responses.csv
Form columns are matched by the text fragments in COLUMN_HINTS (case-insensitive); edit them if
your question wording differs. The script prints which column fed each field.
Rows without consent, or whose village is not in data/villages.csv, are skipped (never guessed).
Answers that cannot be parsed stay empty (unknown) so F1 can ask for them later.
"""
import argparse
import hashlib
import re
from pathlib import Path

import pandas as pd

from common import DATA, read_villages, write_csv
from features.f1_interview import build_profile_text

COLUMN_HINTS = {
    "timestamp": ["timestamp"],
    "village": ["village"],
    "consent": ["consent"],
    "primary_cultivator_gender": ["mainly does the farming", "main farming person"],
    "tenure": ["owned, rented", "owned or rented", "sharecropped"],
    "acres": ["how many acres"],
    "crops_last_season": ["which crops"],
    "irrigation": ["rain only", "well or borewell"],
    "water_hours_per_week": ["hours of water"],
    "cattle_count": ["cows", "buffalo"],
    "goats_count": ["goats"],
    "fodder_weeks_left": ["weeks of fodder"],
    "off_farm_income": ["other income"],
    "loan_due_month": ["loan repayment", "loan"],
}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def match_columns(columns) -> dict[str, str]:
    found = {}
    for field, hints in COLUMN_HINTS.items():
        for col in columns:
            if any(h in col.lower() for h in hints) and col not in found.values():
                found[field] = col
                break
    return found


def number(v):
    m = re.search(r"\d+(\.\d+)?", str(v)) if pd.notna(v) else None
    return float(m.group()) if m else None


def whole(v):
    n = number(v)
    return int(n) if n is not None else None


def pick(v, options: dict[str, str]):
    low = str(v).lower() if pd.notna(v) else ""
    for needle, value in options.items():
        if needle in low:
            return value
    return None


def month(v):
    low = str(v).lower() if pd.notna(v) else ""
    for name, i in MONTHS.items():
        if name in low:
            return i
    return None


def text(v):
    return str(v).strip() if pd.notna(v) and str(v).strip() else None


def is_yes(v) -> bool:
    return str(v).strip().lower() in {"yes", "y", "true", "1", "हो", "हां", "हाँ", "होय"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("form_csv", type=Path)
    args = ap.parse_args()

    form = pd.read_csv(args.form_csv)
    cols = match_columns(form.columns)
    for field in COLUMN_HINTS:
        print(f"  {field:28s} <- {cols.get(field, '(not found: stays unknown)')}")
    for required in ("village", "consent"):
        if required not in cols:
            raise SystemExit(f"form has no '{required}' column; edit COLUMN_HINTS")

    villages = read_villages()
    by_name = {n.strip().lower(): int(i) for n, i in zip(villages["name"], villages["village_id"])}
    get = lambda row, f: row[cols[f]] if f in cols else None  # noqa: E731

    rows, skipped = [], 0
    for idx, row in form.iterrows():
        village_id = by_name.get(str(get(row, "village")).strip().lower())
        if not is_yes(get(row, "consent")) or village_id is None:
            skipped += 1
            continue
        key = f"{get(row, 'timestamp') or idx}|{get(row, 'village')}"
        hh = {
            "hh_id": "SRV-" + hashlib.sha1(key.encode()).hexdigest()[:8],
            "village_id": village_id,
            "tenure": pick(get(row, "tenure"), {"share": "sharecrop", "rent": "tenant", "lease": "tenant", "own": "owner"}),
            "acres": number(get(row, "acres")),
            "irrigation": pick(get(row, "irrigation"), {"bore": "borewell", "well": "well", "rain": "rainfed"}),
            "water_hours_per_week": number(get(row, "water_hours_per_week")),
            "crops_last_season": text(get(row, "crops_last_season")),
            "cattle_count": whole(get(row, "cattle_count")),
            "goats_count": whole(get(row, "goats_count")),
            "fodder_weeks_left": number(get(row, "fodder_weeks_left")),
            "off_farm_income": text(get(row, "off_farm_income")),
            "loan_due_month": month(get(row, "loan_due_month")),
            "cash_buffer_band": None,  # not asked in the survey; F1 asks it
            "primary_cultivator_gender": pick(get(row, "primary_cultivator_gender"),
                                              {"both": "both", "woman": "woman", "female": "woman", "man": "man", "male": "man"}),
            "consent": True,
            "is_synthetic": False,
        }
        if hh["irrigation"] == "rainfed" and hh["water_hours_per_week"] is None:
            hh["water_hours_per_week"] = 0.0
        hh["profile_text_norm"] = build_profile_text(hh)
        rows.append(hh)

    write_csv(pd.DataFrame(rows), DATA / "survey_households.csv",
              [f"REAL survey data imported from {args.form_csv.name} by scripts/import_survey.py; consented rows only"])
    print(f"wrote {len(rows)} survey households; skipped {skipped} (no consent or unknown village)")


if __name__ == "__main__":
    main()
