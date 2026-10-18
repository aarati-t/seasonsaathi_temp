# SeasonSaathi 🌾

**An AI climate-risk companion for drought-hit Marathwada farming households, built for NGO field agents.**

It answers the hardest questions a smallholder faces each season: *what to sow this rabi, and whether to keep or sell
cattle in a drought.* It profiles the cultivating household (including tenants and women cultivators), not just the
landowner. Every answer is grounded in real facts, checked by a verifier, and honest about its confidence.

- **Live app:** https://seasonsaathi-184136561293.asia-south1.run.app (presenter mode: `/?demo=1`)
- **Stack:** Gemini on Vertex AI · Agent Development Kit (ADK) · Earth Engine · BigQuery · Cloud Run · Pub/Sub · Cloud Storage

---

## What it does

| Feature | What the field agent gets |
|---|---|
| **F1 Interview** | Talk or type in Marathi, Hindi or English. Gemini transcribes audio, extracts the profile, and asks **at most 3 questions**, only those that change the risk. Every field must match the farmer's own words (no guessing). Consent is required before saving. |
| **F3 Crop-mix plan** | Compares 3 rabi options (Chana only / Chana + safflower / Jowar + fodder) with an **ACT / WAIT / AVOID** signal, water fit, cash at risk, confidence and reasons, each citing a fact. |
| **F4 Livestock & fodder** | Keep and buy fodder / sell one / sell now, with ₹ cash needed, animal value kept and sale proceeds computed in code from sourced prices, plus hand-checked support pointers (fodder camps, MGNREGA). |
| **F2 Season watch** | Plot-level stress from CHIRPS rain and Sentinel-2 NDVI. An agent picks the next step and writes a 2-line Marathi/Hindi alert, published through **Pub/Sub**. |
| **F7 Learning loop** | Recorded outcomes recalibrate confidence: HIGH is lowered to MED when past HIGH calls in that context were often wrong. Feedback and local-term review are included. |
| **Impact page** | KPIs computed in code and labelled honestly (SIMULATED / TESTED / REPLAY / MEASURED / TARGET). |

UI: mobile-first, EN / मराठी / हिंदी, voice input, read-aloud, and an **Agent Trace** panel showing every tool call,
fact, verifier step and engine used.

## Why it is different

1. **Tools return facts, Gemini judges, code verifies.** Tools return values with a `fact_id` and source, never
   decisions. Gemini compares the options. A code verifier rejects any claim citing an unknown fact or a wrong number,
   and a verifier agent checks that each claim follows from its fact. On failure the answer is regenerated once; if it
   still fails, the recommendation is **withheld**.
2. **Grounded in real data.** Earth Engine CHIRPS rainfall, Sentinel-2 NDVI, SMAP root-zone soil moisture and
   OpenLandMap soil properties. A water budget (root-zone water + irrigation vs a sourced crop water need) is computed
   in code, and Agmarknet mandi prices and official MSP are used for cash risk.
3. **Safety in code.** A short water budget can never produce "go ahead" (ACT). Unknown cash counts as low.
4. **Honest confidence.** Calibration from past outcomes; synthetic and demo outcomes are labelled and never counted as
   observed; missing data lowers confidence and is listed as a data gap; SYNTHETIC badges everywhere.
5. **Never blocks the demo.** ADK engine → plain engine → cached result → rule result (confidence LOW).

## Architecture

```
UI (HTML + Tailwind + vanilla JS, served by FastAPI)  ──>  FastAPI on Cloud Run
   ├── Engine selector: ADK SequentialAgent [planner → assessor → verifier]  (fallback: plain google-genai engine)
   ├── Tools (facts only): rain · crop health · similar households (Vertex embeddings) · prices · calibration
   │                       · soil status · water budget · livestock
   ├── Code: verifier · safety floor · calibration rule · explainability
   ├── Data: Postgres locally / BigQuery on GCP (same tables, repo/base.py)
   ├── Pub/Sub: stress-alerts → push → /api/alerts/receive
   └── Cloud Storage: audio recordings
```

| Google Cloud | Used for |
|---|---|
| Gemini on Vertex AI | All agent reasoning (interview, crop plan, livestock, alerts, transcription), embeddings |
| ADK | Multi-agent orchestration with function tools |
| Earth Engine | CHIRPS, Sentinel-2 NDVI, SMAP soil moisture, OpenLandMap soil (exported per village / plot) |
| BigQuery | All tables, calibration view, decision log, impact runs |
| Cloud Run | One service: UI + API + agents |
| Pub/Sub | Event-driven stress alerts |
| Cloud Storage | Voice recordings |

## Impact (KPIs)

Run `python scripts/run_impact.py`. Cached replay: 0 model calls, about 13 s.

| KPI | Status | Result |
|---|---|---|
| K4 Verifier test | TESTED | **20/20** planted errors caught (changed numbers, missing/invented fact IDs, unsupported claims) · **10/10** valid controls accepted |
| K3 Satellite replay | REPLAY | 31/31 plots reached HIGH stress, median **13 days** before the (unverified) reference date |
| K2 Water-short plans | SIMULATED | In this drought year almost every rabi option is water-short, so the system says WAIT instead of pushing a crop |
| K5 Confidence gap | SIMULATED | 0.339 → 0.225 after the calibration rule (synthetic, in-sample) |
| K6 Coverage + runtime | MEASURED | 15/31 demo households tenant/sharecropper or woman-cultivated; p50 13.5 s per decision |
| K1, K7 | SIMULATED / TARGET | See `tests/README_P5b.md` for definitions and limitations |

## Data honesty

- **Real:** CHIRPS rain, Sentinel-2 NDVI, SMAP soil moisture, OpenLandMap soil, Agmarknet prices (Dharashiv), MSP,
  sourced fodder/cattle price reports, crop water needs (ICAR-IIMR, published studies), FAO-56 soil tables,
  hand-entered village coordinates.
- **Synthetic (badged):** demo households, past outcomes, plot locations near villages, normal animal values.
- No invented coordinates, prices or dataset values. Missing data is shown as "not available".

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env              # GENAI_MODE=vertex, GOOGLE_CLOUD_PROJECT, GEMINI_MODEL, EMBED_MODEL
gcloud auth application-default login
createdb seasonsaathi
python scripts/load_data.py       # Postgres; data exports already in data/
python scripts/embed_profiles.py  # Vertex embeddings
uvicorn app.main:app --port 8000  # http://localhost:8000/?demo=1
pytest -q                         # 181 unit tests (Gemini tests: pytest -m llm)
```
`LLM_OFFLINE=true` in `.env` runs the UI without any Gemini calls (cached or rule results).

Data refresh (optional): `scripts/ee_export.py` (Earth Engine), `scripts/prep_prices.py`, `scripts/make_synthetic.py`.

## Deploy (Cloud Run + BigQuery)

See `DEPLOY.md`. In short:
```bash
GOOGLE_CLOUD_PROJECT=$PROJECT BQ_DATASET=seasonsaathi BQ_LOCATION=$REGION python scripts/load_data.py --backend bigquery
./deploy.sh                       # service account auth on Cloud Run: no keys deployed
scripts/smoke_test.sh $URL
```

## Repository

```
app/            FastAPI routes + static UI (index.html, app.js, i18n/)
features/       f1_interview, f2_watch, f3_cropmix, f4_livestock, learning, explain, impact, verifier_bench
orchestrators/  adk_engine (primary), plain_engine (fallback), selector (fallback chain + decision log)
tools/          facts only: rain, ndvi, peers, prices, calibration, soil, water_budget, livestock, stress
core/           schemas, verifier, safety_floor, llm (Vertex client, cache, backoff), settings
prompts/        planner, assessor, verifier, interviewer, extractor, F2/F4 prompts
repo/           postgres / bigquery / csv backends behind one interface
config/         thresholds, terms glossary, support options, soil water (FAO-56), impact assumptions
db/             schema.sql, BigQuery schema, calibration view
scripts/        data export/load, run_f1..f4, run_impact, smoke test, sync to BigQuery
tests/          unit tests, fixtures, manual check lists (README_P2..P7)
SPEC.md         the build specification (source of truth)
```

## Roadmap

- Field survey (Ganjam, Odisha): cattle are grazed and goats are preferred → value goats, record grazing,
  add new villages and regions.
- 7-14 day weather outlook, Soil Health Card photo → N/P/K facts, fertilizer use and official prices.
- F5 store/sell/keep, F6 loss evidence pack, F8 village view, IVR/SMS for farmers without smartphones.
- Replace synthetic outcomes with recorded field outcomes to validate calibration.
