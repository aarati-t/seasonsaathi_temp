# P7 checks (soil + water-budget grounding, SPEC F3 step 1 / §10.1 step 4b)

F3 now cites soil and water facts:
- `get_soil_status(hh_id)`: soil map at the plot (OpenLandMap: texture, clay, sand, organic carbon, pH, field capacity),
  root-zone and topsoil moisture (SMAP), and the farmer's own soil words (F1 `soil_type_farmer`).
- `get_water_budget(hh_id)`: per option, water available (root-zone water above wilting point + rabi irrigation) as a
  % of the crop's sourced need (`data/crop_water_need.csv`). Arithmetic in code; missing input -> "not available".
- Code verifier: water_fit must match the coverage band (short < 70 %, tight 70-100 %, fit >= 100 %; assumptions in
  `config/thresholds.yaml`). An option without coverage loses one confidence level ("water budget not available").

## 0. Tests
| Command | Vertex? | Expected |
|---|---|---|
| `pytest -m unit` | no | `166 passed` (P7 adds `tests/test_water_budget.py`: 9) |

## 1. Data (run once, Earth Engine login as for P1)
```bash
python scripts/ee_export.py --only soil      # SMAP sm_surface + sm_rootzone per village, OpenLandMap soil per plot
python scripts/load_data.py                  # loads soil_moisture (+ sm_rootzone), soil_props, crop_water_need
psql seasonsaathi -c "select texture_class, count(*) from soil_props group by 1"
psql seasonsaathi -c "select village_id, max(date), max(sm_rootzone) from soil_moisture group by 1"
```
Expect `wrote 40 soil moisture rows`, `wrote 31 soil property rows ({...})`, and a texture count per class.

## 2. Tool facts (no Gemini)
```bash
python -c "
from repo.base import get_repo; from tools import soil, water_budget as wb
r = get_repo()
for f in soil.get_soil_status('HH-DEMO-FLOOR', r) + wb.get_water_budget('HH-DEMO-FLOOR', r): print(f['fact_id'], '|', f['text'])"
```
Expect F-soil-texture / F-soil-fc / F-soil-oc / F-soil-ph / F-soil-rootzone, F-wb-need-* with source rows, and
F-wb-coverage-* with "covers about X-Y% of the crop's need". Texture classes not in FAO-56 Table 19 (e.g. clay loam)
give "not available" for that plot, by design.

## 3. F3 in the UI (LLM_OFFLINE=false)
- `#/f3/HH-DEMO-FLOOR`: Agent Trace shows 🛰️ get_soil_status + get_water_budget; each option has a driver citing
  F-wb-coverage-…; facts table lists soil + water rows with source links; water_fit matches the coverage band.
- Before the soil export: banner-free, but trace shows "Confidence lowered: water budget not available".

## 4. F1 soil words
New household: "मी बटाईने शेती करते, काळी माती आहे, 2 एकर" → profile chip `soil_type_farmer: black cotton soil (vertisol)`;
after saving, F3 uses it (no plot → soil map unavailable, but the water budget uses FAO-56 clay for vertisols, labelled).
