"""Tool: get_crop_health(plot_id). Facts from ndvi_series (Sentinel-2 via Earth Engine). No decisions."""
from repo.base import Repo, get_repo

SOURCE = "Sentinel-2 SR (COPERNICUS/S2_SR_HARMONIZED) via Earth Engine; plot location SYNTHETIC"
SOURCE_URL = "https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED"


def get_crop_health(plot_id: str, repo: Repo | None = None) -> list[dict]:
    repo = repo or get_repo()
    rows = sorted(repo.fetch_where("ndvi_series", plot_id=plot_id), key=lambda r: r["date"])
    plot = repo.fetch_where("plots", plot_id=plot_id)
    data_source = sorted({r.get("source") or "unknown" for r in rows}) if rows else ["unknown"]
    synthetic_values = "SYNTHETIC" in data_source
    base = {"source": "SYNTHETIC NDVI (not Sentinel-2)" if synthetic_values else SOURCE, "source_url": SOURCE_URL,
            "is_synthetic": synthetic_values or bool(plot and plot[0]["is_synthetic"]),
            "data_source": "/".join(data_source)}
    clear = [r for r in rows if r["ndvi"] is not None and r["ndvi_normal"]]
    if not clear:
        return [{**base, "fact_id": "F-ndvi-none", "value": None, "unit": None,
                 "text": f"No cloud-free NDVI for plot {plot_id}"}]
    r = clear[-1]
    pct = round(r["ndvi"] / r["ndvi_normal"] * 100, 1)
    cloudy = sum(1 for x in rows if x["ndvi"] is None)
    return [
        {**base, "fact_id": "F-ndvi-latest", "value": round(r["ndvi"], 3), "unit": "NDVI", "as_of": str(r["date"]),
         "text": f"NDVI {round(r['ndvi'], 3)} for the 10 days from {r['date']} (normal {round(r['ndvi_normal'], 3)})"},
        {**base, "fact_id": "F-ndvi-pct-normal", "value": pct, "unit": "% of normal", "as_of": str(r["date"]),
         "text": f"Crop greenness is {pct}% of normal for the 10 days from {r['date']}"},
        {**base, "fact_id": "F-ndvi-cloudy", "value": cloudy, "unit": "windows",
         "text": f"{cloudy} of {len(rows)} ten-day windows had no cloud-free view"},
    ]
