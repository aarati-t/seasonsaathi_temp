"""Earth Engine export (SPEC 10.1): CHIRPS rain, Sentinel-2 NDVI, SMAP soil moisture, OpenLandMap soil -> data/*.csv.

Usage: python scripts/ee_export.py [--year 2026] [--only rain|ndvi|soil] [--soil]
       --only soil exports SMAP soil moisture (surface + root zone) and OpenLandMap soil properties per plot (P7).
Needs: earthengine authenticate, and EE_PROJECT (or GOOGLE_CLOUD_PROJECT) registered for Earth Engine.
Reads data/villages.csv and data/plots.csv. Missing values stay empty; gaps are never filled.
"""
import argparse
import os
from datetime import date, datetime, timedelta, timezone

import ee
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from common import DATA, read_csv, read_villages, write_csv

CHIRPS = "UCSB-CHG/CHIRPS/DAILY"
S2 = "COPERNICUS/S2_SR_HARMONIZED"
SMAP = "NASA/SMAP/SPL4SMGP/008"
SMAP_BANDS = ["sm_surface", "sm_rootzone"]          # 0-5 cm and 0-100 cm volume fraction
OLM = "OpenLandMap/SOL/"                             # 250 m; bands b0/b10/b30 = depth in cm
OLM_TEXTURE = OLM + "SOL_TEXTURE-CLASS_USDA-TT_M/v02"
OLM_CLAY = OLM + "SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02"          # % (kg/kg)
OLM_SAND = OLM + "SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02"          # % (kg/kg)
OLM_OC = OLM + "SOL_ORGANIC-CARBON_USDA-6A1C_M/v02"              # x 5 g/kg
OLM_PH = OLM + "SOL_PH-H2O_USDA-4C1A2A_M/v02"                    # pH x 10
OLM_FC = OLM + "SOL_WATERCONTENT-33KPA_USDA-4B1C_M/v01"          # volumetric % at 33 kPa (field capacity)
# USDA texture class codes as listed in the Earth Engine catalog (Cl, SiCl, SaCl, ClLo, SiClLo, SaClLo, Lo, SiLo,
# SaLo, Si, LoSa, Sa); names match config/soil_water.yaml keys.
TEXTURE = {1: "clay", 2: "silty clay", 3: "sandy clay", 4: "clay loam", 5: "silty clay loam", 6: "sandy clay loam",
           7: "loam", 8: "silt loam", 9: "sandy loam", 10: "silt", 11: "loamy sand", 12: "sand"}
TOP_BANDS = ["b0", "b10", "b30"]                     # 0-30 cm
RAIN_NORMAL_YEARS = range(2014, 2024)   # 2014-2023
NDVI_NORMAL_YEARS = range(2019, 2024)   # 2019-2023
VILLAGE_BUFFER_M = 5000
PLOT_BUFFER_M = 30
BATCH = 20
SCL_EXCLUDE = [3, 8, 9, 10, 11]  # cloud shadow, cloud medium/high, cirrus, snow


def init_ee() -> None:
    load_dotenv()
    project = os.getenv("EE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("Set EE_PROJECT (or GOOGLE_CLOUD_PROJECT) in .env to your Earth Engine project id")
    ee.Initialize(project=project)


def batches(df: pd.DataFrame):
    for i in range(0, len(df), BATCH):
        yield df.iloc[i:i + BATCH]


def to_fc(df: pd.DataFrame, id_col: str, buffer_m: int) -> ee.FeatureCollection:
    return ee.FeatureCollection([
        ee.Feature(ee.Geometry.Point([float(r.lon), float(r.lat)]).buffer(buffer_m), {id_col: getattr(r, id_col)})
        for r in df.itertuples()
    ])


def reduce_collection(images: ee.ImageCollection, fc: ee.FeatureCollection, scale: int) -> list[dict]:
    """Mean of each image over each feature; returns plain property dicts (date + band means)."""
    def per_image(img):
        reduced = img.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=scale)
        return reduced.map(lambda f: f.set("date", img.get("date")))
    return [f["properties"] for f in images.map(per_image).flatten().getInfo()["features"]]


def empty_band(name: str) -> ee.Image:
    """Fully masked image so a median over zero images yields 'no value', not an error."""
    return ee.Image.constant(0).toFloat().rename(name).updateMask(0)


def last_available(collection_id: str, start: date) -> date:
    ms = ee.ImageCollection(collection_id).filterDate(start.isoformat(), (date.today() + timedelta(1)).isoformat()) \
        .aggregate_max("system:time_start").getInfo()
    if ms is None:
        raise SystemExit(f"{collection_id}: no images since {start}")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


# ---------- Rainfall ----------

def add_cum_deficit(df: pd.DataFrame) -> pd.DataFrame:
    """cum_deficit_pct = (cum normal - cum rain) / cum normal * 100 since season start.
    Positive = deficit. After any missing day the cumulative value is unknown, so it stays empty."""
    out = []
    for _, g in df.sort_values(["village_id", "date"]).groupby("village_id"):
        g = g.copy()
        gap = (g["rain_mm"].isna() | g["normal_mm"].isna()).cummax()
        cum_rain, cum_norm = g["rain_mm"].cumsum(), g["normal_mm"].cumsum()
        deficit = (cum_norm - cum_rain) / cum_norm.replace(0, np.nan) * 100
        g["cum_deficit_pct"] = deficit.where(~gap).round(1)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def export_rain(villages: pd.DataFrame, season_start: date) -> None:
    last = last_available(CHIRPS, season_start)
    print(f"CHIRPS: last available date {last}")
    chirps = ee.ImageCollection(CHIRPS).select("precipitation")
    normal_years = ee.List(list(RAIN_NORMAL_YEARS))

    def with_normal(img):
        d = ee.Date(img.get("system:time_start"))
        same_days = ee.ImageCollection(normal_years.map(
            lambda y: chirps.filterDate(
                ee.Date.fromYMD(y, d.get("month"), d.get("day")),
                ee.Date.fromYMD(y, d.get("month"), d.get("day")).advance(1, "day"),
            ).first()
        ))
        return img.rename("rain").addBands(same_days.mean().rename("normal")).set("date", d.format("YYYY-MM-dd"))

    images = chirps.filterDate(season_start.isoformat(), (last + timedelta(1)).isoformat()).map(with_normal)
    rows = []
    for b in batches(villages):
        for p in reduce_collection(images, to_fc(b, "village_id", VILLAGE_BUFFER_M), scale=5566):
            rows.append({"village_id": p["village_id"], "date": p["date"],
                         "rain_mm": p.get("rain"), "normal_mm": p.get("normal")})
    df = add_cum_deficit(pd.DataFrame(rows))
    df["source"] = "CHIRPS"
    df[["rain_mm", "normal_mm"]] = df[["rain_mm", "normal_mm"]].apply(pd.to_numeric).round(2)
    write_csv(df, DATA / "rain_series.csv", [
        f"dataset: {CHIRPS} band precipitation (mm/day); mean over {VILLAGE_BUFFER_M} m village buffer",
        f"normal: same calendar day mean {RAIN_NORMAL_YEARS[0]}-{RAIN_NORMAL_YEARS[-1]}",
        f"date range: {season_start} to {last} (last available CHIRPS date)",
        f"exported: {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} by scripts/ee_export.py",
    ])
    print(f"wrote {len(df)} rain rows")


# ---------- NDVI ----------

def s2_ndvi(start: date, end: date, region) -> ee.Image:
    def masked_ndvi(img):
        scl = img.select("SCL")
        ok = ee.Image.constant(1)
        for c in SCL_EXCLUDE:
            ok = ok.And(scl.neq(c))
        # toFloat: normalizedDifference is Float<-1,1>; plain float matches empty_band for merging
        return img.updateMask(ok).normalizedDifference(["B8", "B4"]).toFloat().rename("ndvi")
    coll = (ee.ImageCollection(S2).filterBounds(region)
            .filterDate(start.isoformat(), end.isoformat())
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
            .map(masked_ndvi))
    return coll.merge(ee.ImageCollection([empty_band("ndvi")])).median()


def windows(start: date, step_days: int) -> list[tuple[date, date]]:
    """[start, end) windows from season start up to today; the last one may be partial."""
    out, today = [], date.today()
    while start <= today:
        out.append((start, min(start + timedelta(step_days), today + timedelta(1))))
        start += timedelta(step_days)
    return out


def shift_year(d: date, year: int) -> date:
    return d.replace(year=year)


def export_ndvi(plots: pd.DataFrame, season_start: date) -> None:
    rows = []
    wins = windows(season_start, 10)
    for b in batches(plots):
        fc = to_fc(b, "plot_id", PLOT_BUFFER_M)
        region = fc.geometry()
        images = []
        for s, e in wins:
            normal = ee.ImageCollection([
                s2_ndvi(shift_year(s, y), shift_year(s, y) + (e - s), region) for y in NDVI_NORMAL_YEARS
            ]).median()
            images.append(s2_ndvi(s, e, region).addBands(normal.rename("ndvi_normal")).set("date", s.isoformat()))
        for p in reduce_collection(ee.ImageCollection(images), fc, scale=10):
            rows.append({"plot_id": p["plot_id"], "date": p["date"],
                         "ndvi": p.get("ndvi"), "ndvi_normal": p.get("ndvi_normal")})
    df = pd.DataFrame(rows).sort_values(["plot_id", "date"])
    df["source"] = "Sentinel-2"
    df[["ndvi", "ndvi_normal"]] = df[["ndvi", "ndvi_normal"]].apply(pd.to_numeric).round(4)
    write_csv(df, DATA / "ndvi_series.csv", [
        f"dataset: {S2}; NDVI = (B8 - B4)/(B8 + B4); CLOUDY_PIXEL_PERCENTAGE < 40; SCL mask {SCL_EXCLUDE}",
        f"10-day median windows (date = window start), mean over {PLOT_BUFFER_M} m plot buffer",
        f"normal: median of the same window in {NDVI_NORMAL_YEARS[0]}-{NDVI_NORMAL_YEARS[-1]}",
        f"date range: {season_start} to {date.today()}; empty = no cloud-free pixels",
        "plot locations are SYNTHETIC points near villages (data/plots.csv); NDVI values are real",
        f"exported: {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} by scripts/ee_export.py",
    ])
    print(f"wrote {len(df)} ndvi rows")


# ---------- Soil moisture and soil properties (P7) ----------

def export_soil(villages: pd.DataFrame, season_start: date) -> None:
    print(f"SMAP: last available date {last_available(SMAP, season_start)}")
    smap = ee.ImageCollection(SMAP).select(SMAP_BANDS).map(lambda img: img.toFloat())
    empty = ee.ImageCollection([empty_band(SMAP_BANDS[0]).addBands(empty_band(SMAP_BANDS[1]))])
    images = ee.ImageCollection([
        smap.filterDate(s.isoformat(), e.isoformat()).merge(empty).mean().set("date", s.isoformat())
        for s, e in windows(season_start, 7)
    ])
    rows = []
    for b in batches(villages):
        for p in reduce_collection(images, to_fc(b, "village_id", VILLAGE_BUFFER_M), scale=11000):
            rows.append({"village_id": p["village_id"], "date": p["date"], **{k: p.get(k) for k in SMAP_BANDS}})
    df = pd.DataFrame(rows).sort_values(["village_id", "date"])
    df[SMAP_BANDS] = df[SMAP_BANDS].apply(pd.to_numeric).round(4)
    write_csv(df, DATA / "soil_moisture.csv", [
        f"dataset: {SMAP} bands sm_surface (0-5 cm) and sm_rootzone (0-100 cm), m3/m3",
        f"weekly mean (date = week start) over {VILLAGE_BUFFER_M} m village buffer (SMAP pixel 11 km); empty = no image",
        f"date range: {season_start} to {date.today()}",
        f"exported: {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} by scripts/ee_export.py",
    ])
    print(f"wrote {len(df)} soil moisture rows")


def soil_image() -> ee.Image:
    top = lambda dataset, name: ee.Image(dataset).select(TOP_BANDS).reduce(ee.Reducer.mean()).rename(name)
    return ee.Image.cat([ee.Image(OLM_TEXTURE).select("b10").rename("texture"), top(OLM_CLAY, "clay"),
                         top(OLM_SAND, "sand"), top(OLM_OC, "oc"), top(OLM_PH, "ph"), top(OLM_FC, "fc")])


def export_soil_props(plots: pd.DataFrame) -> None:
    rows = []
    img = soil_image()
    for b in batches(plots):
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([float(r.lon), float(r.lat)]), {"plot_id": r.plot_id})
                                   for r in b.itertuples()])
        for f in img.reduceRegions(collection=fc, reducer=ee.Reducer.first(), scale=250).getInfo()["features"]:
            p = f["properties"]
            num = lambda k, factor=1.0: None if p.get(k) is None else round(float(p[k]) * factor, 2)
            code = p.get("texture")
            rows.append({"plot_id": p["plot_id"], "texture_class": TEXTURE.get(int(code)) if code is not None else None,
                         "clay_pct": num("clay"), "sand_pct": num("sand"),
                         "organic_carbon_pct": num("oc", 5 / 10),   # x 5 g/kg -> g/kg -> %
                         "ph": num("ph", 1 / 10), "water_fc_pct": num("fc"), "depth_cm": 30})
    df = pd.DataFrame(rows).sort_values("plot_id")
    synthetic = dict(zip(plots["plot_id"], plots["is_synthetic"]))
    df["source"] = "OpenLandMap soil maps via Earth Engine (250 m; texture at 10 cm, others mean of 0/10/30 cm)"
    df["source_url"] = "https://developers.google.com/earth-engine/datasets/catalog/OpenLandMap_SOL_SOL_TEXTURE-CLASS_USDA-TT_M_v02"
    df["is_synthetic"] = df["plot_id"].map(synthetic).fillna(True)
    write_csv(df, DATA / "soil_props.csv", [
        f"datasets: {OLM_TEXTURE} (b10), {OLM_CLAY}, {OLM_SAND}, {OLM_OC} (x5 g/kg -> %), {OLM_PH} (/10), {OLM_FC} (vol %)",
        "values at the plot point (250 m pixel); clay/sand/OC/pH/field capacity = mean of 0, 10, 30 cm bands",
        "is_synthetic follows the plot: plot locations are SYNTHETIC points near villages, soil values are real map values",
        f"exported: {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} by scripts/ee_export.py",
    ])
    print(f"wrote {len(df)} soil property rows ({df['texture_class'].value_counts().to_dict()})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=date.today().year, help="season year (starts 1 Jun)")
    ap.add_argument("--only", choices=["rain", "ndvi", "soil"])
    ap.add_argument("--soil", action="store_true", help="also export SMAP soil moisture and OpenLandMap soil properties")
    args = ap.parse_args()

    init_ee()
    season_start = date(args.year, 6, 1)
    villages = read_villages()
    if args.only in (None, "rain"):
        export_rain(villages, season_start)
    if args.only in (None, "ndvi"):
        plots_path = DATA / "plots.csv"
        if not plots_path.exists():
            raise SystemExit("data/plots.csv missing: run scripts/make_synthetic.py first")
        export_ndvi(read_csv(plots_path), season_start)
    if args.only == "soil" or (args.only is None and args.soil):
        export_soil(villages, season_start)
        export_soil_props(read_csv(DATA / "plots.csv"))


if __name__ == "__main__":
    main()
