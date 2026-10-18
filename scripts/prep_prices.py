"""Mandi prices (SPEC 10.2): Agmarknet downloads + official MSP -> data/prices.csv, data/price_summary.csv.

Usage: python scripts/prep_prices.py
Reads:
  data/raw/agmarknet_<district>_<crop>.csv  Agmarknet "Market Wise Price & Arrival" exports. Title rows
                                           before the real header are skipped. These exports hold one
                                           district-level price per day (no market column), so market
                                           stays empty and n_markets is unknown.
  data/raw/msp_2026.csv                    official MSP (verified against PIB); values used unchanged
  config/commodities.yaml                  Agmarknet name -> our key, and which MSP row to compare to
  data/raw/livestock_prices.csv            fodder/cattle prices and norms (item_key rows); values used
                                           unchanged. basis=assumption rows must be is_synthetic=true.
Prices are Rs/quintal. Rows with a missing or <= 0 price are dropped. Nothing is generated:
a commodity with no mandi data gets a summary row with an empty price and an "MSP only" note.
"""
import csv
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from common import DATA, ROOT, read_csv, write_csv

RAW = DATA / "raw"
CONFIG = ROOT / "config" / "commodities.yaml"
AGMARKNET_URL = "https://agmarknet.gov.in/home"
DEFAULT_MISSING_NOTE = "no mandi data; MSP only"
PRICE_COLS = ["commodity", "district", "market", "date", "modal_price", "reference_price", "reference_type",
              "reference_season", "pct_vs_reference", "source", "source_url", "is_synthetic"]
SUMMARY_COLS = ["commodity", "district", "latest_date", "latest_modal_price", "reference_price", "reference_type",
                "reference_season", "reference_source_url", "pct_vs_reference", "n_markets", "note", "source",
                "source_url", "is_synthetic"]
LIVESTOCK_COLS = ["item_key", "item", "unit", "region", "period", "value_low", "value_high", "value_mid",
                  "basis", "is_synthetic", "note", "source_url"]
BASIS = {"reported", "derived", "assumption"}
PRICE_HEADER = re.compile(r"price on (\d{1,2} \w{3}, \d{4})", re.I)


def squash(name: str) -> str:
    return re.sub(r"\s+", "", str(name)).lower()


def load_commodities(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text())


def build_name_map(commodities: dict) -> dict[str, str]:
    return {squash(n): key for key, c in commodities.items() for n in c["agmarknet_names"]}


def normalise_commodity(raw: str, name_map: dict[str, str]) -> str | None:
    return name_map.get(squash(raw))


def pct_vs_reference(price, reference):
    if price is None or reference is None or pd.isna(price) or pd.isna(reference) or reference == 0:
        return None
    return round((price - reference) / reference * 100, 1)


def to_price(cell: str) -> float | None:
    try:
        v = float(str(cell).replace(",", "").strip())
    except ValueError:
        return None  # "-" or blank = no data that day
    return v if v > 0 else None


def read_agmarknet(path: Path, name_map: dict[str, str], unknown: set) -> tuple[list[dict], int]:
    """Returns (rows kept, rows dropped). One row per commodity x 'Price on <date>' column."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = list(csv.reader(f))
    header_idx = next((i for i, r in enumerate(lines) if "Commodity" in [c.strip() for c in r]), None)
    if header_idx is None:
        raise SystemExit(f"{path.name}: no header row with a 'Commodity' column")
    header = [c.strip() for c in lines[header_idx]]
    commodity_col = header.index("Commodity")
    date_cols = {i: datetime.strptime(m.group(1), "%d %b, %Y").date()
                 for i, h in enumerate(header) if (m := PRICE_HEADER.search(h))}
    if not date_cols:
        raise SystemExit(f"{path.name}: no 'Price on <date>' columns in {header}")
    district = path.stem.split("_")[1].title()

    kept, dropped = [], 0
    for r in lines[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        key = normalise_commodity(r[commodity_col], name_map)
        if key is None:
            unknown.add(r[commodity_col].strip())
            dropped += len(date_cols)
            continue
        for i, d in date_cols.items():
            price = to_price(r[i]) if i < len(r) else None
            if price is None:
                dropped += 1
                continue
            kept.append({"commodity": key, "district": district, "market": None, "date": d.isoformat(),
                         "modal_price": price})
    return kept, dropped


def read_msp(path: Path) -> dict[str, dict]:
    df = read_csv(path)
    return {r.commodity: {"reference_price": float(r.msp_rs_per_qtl), "reference_type": "MSP",
                          "reference_season": r.marketing_season, "reference_source_url": r.source_url}
            for r in df.itertuples()}


def read_livestock(path: Path) -> pd.DataFrame:
    """Validate and pass through livestock_prices.csv. Values are never changed."""
    df = read_csv(path)
    missing = [c for c in LIVESTOCK_COLS if c not in df.columns]
    if missing:
        raise SystemExit(f"{path.name}: missing columns {missing}")
    df = df[LIVESTOCK_COLS]
    if df.item_key.duplicated().any():
        raise SystemExit(f"{path.name}: duplicate item_key {sorted(df.item_key[df.item_key.duplicated()])}")
    bad_basis = sorted(set(df.basis) - BASIS)
    if bad_basis:
        raise SystemExit(f"{path.name}: basis must be one of {sorted(BASIS)}, got {bad_basis}")
    unlabelled = df[(df.basis == "assumption") & (~df.is_synthetic.astype(bool))]
    if len(unlabelled):
        raise SystemExit(f"{path.name}: basis=assumption rows must have is_synthetic=true: {list(unlabelled.item_key)}")
    if df.value_mid.isna().any():
        raise SystemExit(f"{path.name}: value_mid missing for {list(df.item_key[df.value_mid.isna()])}")
    return df


def summarise(prices: pd.DataFrame, commodities: dict, msp: dict, districts: list[str]) -> pd.DataFrame:
    rows = []
    for key, conf in commodities.items():
        ref = msp.get(conf["msp_key"], {})
        for district in districts:
            g = prices[(prices.commodity == key) & (prices.district == district)]
            row = {"commodity": key, "district": district, **ref, "source": "Agmarknet",
                   "source_url": AGMARKNET_URL, "is_synthetic": False}
            if g.empty:
                row.update(latest_date=None, latest_modal_price=None, pct_vs_reference=None, n_markets=None,
                           note=conf.get("missing_note", DEFAULT_MISSING_NOTE))
            else:
                latest = g[g.date == g.date.max()]
                price = round(float(latest.modal_price.median()), 2)
                markets = latest.market.dropna()
                row.update(latest_date=g.date.max(), latest_modal_price=price,
                           pct_vs_reference=pct_vs_reference(price, ref.get("reference_price")),
                           n_markets=markets.nunique() if len(markets) else None,
                           note=None if len(markets) else "district-level price; markets not listed in export")
            rows.append(row)
    return pd.DataFrame(rows, columns=SUMMARY_COLS)


def run(raw_dir: Path = RAW, out_dir: Path = DATA, config: Path = CONFIG) -> dict:
    commodities = load_commodities(config)
    name_map = build_name_map(commodities)
    msp_path = raw_dir / "msp_2026.csv"
    if not msp_path.exists():
        raise SystemExit(f"{msp_path} missing: reference prices come only from that file")
    msp = read_msp(msp_path)
    for key, c in commodities.items():
        if c["msp_key"] not in msp:
            raise SystemExit(f"{msp_path.name} has no row '{c['msp_key']}' (needed for {key})")

    files = sorted(raw_dir.glob("agmarknet_*.csv"))
    rows, dropped, unknown = [], 0, set()
    for p in files:
        k, d = read_agmarknet(p, name_map, unknown)
        rows += k
        dropped += d
    mandi = pd.DataFrame(rows, columns=["commodity", "district", "market", "date", "modal_price"])
    for col in ["reference_price", "reference_type", "reference_season"]:
        mandi[col] = mandi.commodity.map(lambda k: msp[commodities[k]["msp_key"]][col])
    mandi["pct_vs_reference"] = [pct_vs_reference(p, r) for p, r in zip(mandi.modal_price, mandi.reference_price)]
    mandi["source"], mandi["source_url"], mandi["is_synthetic"] = "Agmarknet", AGMARKNET_URL, False

    prices = mandi.reindex(columns=PRICE_COLS).sort_values(["commodity", "district", "date"], ignore_index=True)
    livestock_path = raw_dir / "livestock_prices.csv"
    livestock = read_livestock(livestock_path) if livestock_path.exists() else None

    districts = sorted(mandi.district.unique())
    summary = summarise(mandi, commodities, msp, districts)
    found = sorted(mandi.commodity.unique())
    missing = [k for k in commodities if k not in found]

    write_csv(prices, out_dir / "prices.csv", [
        "mandi: Agmarknet Market Wise Price & Arrival exports (district-level daily price, Rs/quintal)",
        f"reference: MSP from {msp_path.name} (PIB); pct_vs_reference = (modal - MSP) / MSP * 100",
    ])
    if livestock is not None:
        write_csv(livestock, out_dir / "livestock_prices.csv", [
            f"copied unchanged from data/raw/{livestock_path.name}; basis=assumption rows are SYNTHETIC",
        ])
    write_csv(summary, out_dir / "price_summary.csv", [
        "latest_modal_price = median across markets on latest_date; empty = no mandi data (MSP only)",
    ])
    return {"files": [p.name for p in files], "kept": len(mandi), "dropped": dropped,
            "found": found, "missing": missing, "unknown": sorted(unknown),
            "livestock": None if livestock is None else len(livestock),
            "assumptions": [] if livestock is None else list(livestock.item_key[livestock.basis == "assumption"]),
            "districts": districts}


def main() -> None:
    r = run()
    print(f"files read: {len(r['files'])}  ({', '.join(r['files'])})")
    print(f"mandi rows kept: {r['kept']}  dropped: {r['dropped']} (missing/zero price or unknown name)")
    print(f"districts: {', '.join(r['districts']) or 'none'}")
    print(f"commodities found:   {', '.join(r['found']) or 'none'}")
    print(f"commodities missing: {', '.join(r['missing']) or 'none'} (summary row with MSP only)")
    if r["unknown"]:
        print(f"unknown names (dropped, add to config/commodities.yaml if wanted): {', '.join(r['unknown'])}")
    if r["livestock"] is None:
        print("livestock/fodder: data/raw/livestock_prices.csv not found; skipped")
    else:
        print(f"livestock/fodder rows: {r['livestock']}  (SYNTHETIC assumptions: {', '.join(r['assumptions']) or 'none'})")


if __name__ == "__main__":
    main()
