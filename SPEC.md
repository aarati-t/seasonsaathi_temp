# SeasonSaathi v2: spec for AI-assisted (spec-driven) build
Household climate-risk companion for drought-hit Marathwada
Target: Google Cloud hackathon prototype | Builder: 1 person | Real working time: ~16 hrs
**v2 change:** Gemini on **Vertex AI** and **ADK** are now the primary path. AI Studio API key and the plain orchestrator remain only as fallbacks.

---

## 0. Rules for the AI coding agent
1. Tools return FACTS only (value + fact_id + source). They never return a decision.
2. Gemini (on Vertex AI) makes the judgement and returns JSON matching the schemas in this spec.
3. A verifier step rejects any claim that does not cite a valid `fact_id`.
4. Every mocked value carries `is_synthetic=true` and shows a SYNTHETIC badge in the UI.
5. Never invent coordinates, prices or dataset values.
6. No feature may block the demo. Fallback chain: ADK engine → plain engine → cached result → rule result with `confidence: LOW`.
7. Use the `google-genai` SDK and `google-adk` only. Model name always comes from env `GEMINI_MODEL`.

---

## 1. Scope

| Tier | Feature | Why |
|---|---|---|
| **BUILD (must)** | F1 Household Risk Profile (interview agent) | Fixes gap 6; entry point |
| **BUILD (must)** | F3 Water and Cash Crop-Mix Plan (ADK: planner → assessor → verifier) | Core agentic demo; fixes gaps 1, 2, 5 |
| **BUILD (must)** | F4 Livestock and Fodder Decision | Open space no tool covers; main USP |
| **BUILD (light)** | F2 Season Risk Watch + Pub/Sub alert | Real Earth Engine data, event-driven alert |
| **BUILD (light)** | F7 Calibration loop | Fixes gap 3 |
| **BUILD (optional)** | P-Model: distress-risk predictive model (BigQuery ML, registered in Vertex AI) | Covers "Vertex AI for predictive models" |
| **SCREEN only** | F6 Loss Evidence Pack (with Gemini vision on a crop photo) | Covers "Vision / image analysis" |
| **ROADMAP only** | F5 Store/Sell/Keep, F8 Village view, IVR/SMS | Say it, don't build it |

---

## 2. Google Cloud technology mapping (show this on a slide)

| Suggested technology | Where it is used | Status |
|---|---|---|
| **Gemini on Vertex AI** | All agent reasoning (F1, F3, F4, F2 alert text, F6 drafting) | Core |
| **ADK** | Multi-agent orchestration: SequentialAgent of planner, assessor, verifier with function tools | Core (plain engine = fallback) |
| **Vertex AI embeddings** | Household profile embeddings for semantic similar-household search (F1/F3) | Core |
| **Vertex AI predictive model** | Distress-risk probability from household features (BigQuery ML logistic regression, registered to Vertex AI Model Registry), used as a FACT by the assessor | Optional |
| **BigQuery** | All tables on GCP; calibration view; VECTOR_SEARCH optional | Core |
| **Earth Engine** | CHIRPS rainfall, Sentinel-2 NDVI, SMAP surface + root-zone soil moisture, OpenLandMap soil properties (precomputed export) | Core |
| **Cloud Storage** | Crop photos, audio clips, generated evidence packs | Core |
| **Vision (Gemini multimodal)** | Reads a field photo in F6 / F2 "REQUEST_PHOTOS" step: visible stress, crop stage | Light |
| **Cloud Run** | One FastAPI service (UI + API + ADK engine) | Core |
| **Pub/Sub** | F2 publishes `stress-alerts`; push subscription to `/api/alerts/receive` stores and shows the alert | Light |

**Pitch line:** each technology does a job the solution needs; none is decorative.

---

## 3. Architecture

```
UI (HTML + Tailwind, served by FastAPI)  ──>  FastAPI on Cloud Run
                                               │
                                               ├── Engine selector (ORCHESTRATOR=adk|plain, auto-fallback)
                                               │     ADK: SequentialAgent[
                                               │           planner  (LlmAgent + function tools),
                                               │           assessor (LlmAgent, output_schema = FeatureResult),
                                               │           verifier (LlmAgent) ] + code verifier
                                               │     Plain: same 3 roles as 3 Gemini calls with function calling
                                               │
                                               ├── Tools (shared by both engines)
                                               │     get_rain_status(village_id)           ← rain_series
                                               │     get_crop_health(plot_id)              ← ndvi_series
                                               │     find_similar_households(hh_id)        ← Vertex embeddings + cosine / VECTOR_SEARCH
                                               │     get_soil_status(plot_id)              ← soil_moisture + soil_props + farmer soil type
                                               │     get_water_budget(hh_id)               ← crop_water_need + soil_props + soil_moisture + irrigation
                                               │     get_prices(commodity, district)       ← prices
                                               │     get_calibration(context)              ← calibration view
                                               │     get_distress_probability(hh_id)       ← BQML/Vertex model (optional)
                                               │     analyse_photo(gcs_uri)                ← Gemini vision
                                               │
                                               ├── Pub/Sub: topic stress-alerts → push → /api/alerts/receive
                                               └── Cloud Storage: photos, audio, evidence packs
```

### Dual engine (ADK primary, plain fallback)
- One interface: `Orchestrator.run(feature, hh_id, input) -> FeatureResult`.
- Shared, never duplicated: `tools/*.py`, `core/schemas.py`, `prompts/*.md`, `core/verifier.py`, `core/safety_floor.py`, decision log.
- `orchestrators/adk_engine.py`: ADK `SequentialAgent` with three `LlmAgent`s, tools passed as plain Python functions, session state carries facts between agents. Runs **inside** the FastAPI app via ADK `Runner` + `InMemorySessionService` (no separate `adk deploy` service).
- `orchestrators/plain_engine.py`: same prompts and tools with direct `google-genai` function calling.
- Auto-fallback: ADK error, timeout > 20 s, or schema-invalid output → re-run with plain; log `orchestrator_used` and `fallback_reason`.
- **Both engines are built in the same step (P2)**, so ADK is never the part that gets cut. `tests/test_parity.py`: 3 fixed households → same signal + verifier PASS on both.
- Go/no-go: if ADK fails parity on Cloud Run after 1.5 hrs of debugging, set `ORCHESTRATOR=plain` for the demo and keep ADK code in the repo.
- UI badge: "Engine: ADK on Vertex AI" or "Engine: Plain (fallback)".

---

## 4. Environments and model access

| Concern | LOCAL (laptop) | GCP (demo URL) | Env switch |
|---|---|---|---|
| Gemini | **Vertex AI via ADC** (`gcloud auth application-default login`). Fallback: AI Studio key | **Vertex AI via Cloud Run service account** (role: Vertex AI User) | `GENAI_MODE=vertex\|apikey` |
| Database | PostgreSQL | BigQuery dataset `seasonsaathi` | `DATA_BACKEND=postgres\|bigquery` |
| Similar households | Vertex embeddings stored as float arrays; cosine in numpy | Same (default) or BigQuery `VECTOR_SEARCH` | `VECTOR_MODE=python\|bq` |
| Predictive model | scikit-learn logistic regression on same features | BigQuery ML model (registered to Vertex AI) | `PMODEL_MODE=off\|sklearn\|bqml` |
| Files | `./storage/` | Cloud Storage bucket | `STORAGE_BACKEND=local\|gcs` |
| Alerts | In-process call to the same handler | Pub/Sub topic + push subscription | `ALERTS_MODE=local\|pubsub` |
| Engine | adk / plain | adk / plain | `ORCHESTRATOR=adk\|plain` |

Env vars for Vertex mode (both `google-genai` and ADK read these):
```
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=TRUE      # newer ADK versions also accept GOOGLE_GENAI_USE_ENTERPRISE=TRUE
GEMINI_MODEL=<a current Flash model available in Vertex AI>
EMBED_MODEL=<a current Vertex text-embedding model>
```
Rules:
- All data access goes through `repo/base.py`; implementations `repo/postgres.py`, `repo/bigquery.py`. Same table names and columns.
- Retry with exponential backoff on 429 / RESOURCE_EXHAUSTED. Cache tool results per request.
- Never commit `.env` or keys. Keep `.env.example` current.

---

## 5. Data model

| Table | Key columns | Source |
|---|---|---|
| `villages` | village_id, name, taluka, district, lat, lon | Entered by hand |
| `households` | hh_id, village_id, tenure(owner/tenant/sharecrop), acres, irrigation(rainfed/well/borewell), water_hours_per_week, soil_type_farmer, crops_last_season, cattle_count, goats_count, fodder_weeks_left, off_farm_income, loan_due_month, cash_buffer_band(low/med/high), primary_cultivator_gender, consent(bool), profile_text_norm, profile_embedding, embed_model, is_synthetic | Survey + synthetic |
| `plots` | plot_id, hh_id, lat, lon, is_synthetic | Synthetic points near villages |
| `rain_series` | village_id, date, rain_mm, normal_mm, cum_deficit_pct, source (CHIRPS/SYNTHETIC) | Earth Engine CHIRPS |
| `ndvi_series` | plot_id, date, ndvi, ndvi_normal, source (Sentinel-2/SYNTHETIC) | Earth Engine Sentinel-2 |
| `soil_moisture` | village_id, date, sm_surface, sm_rootzone | Earth Engine SMAP (weekly village mean) |
| `soil_props` | plot_id, texture_class, clay_pct, sand_pct, organic_carbon_pct, ph, water_fc_pct, depth_cm, source, source_url, is_synthetic | Earth Engine OpenLandMap (plot point; is_synthetic follows the plot location) |
| `crop_water_need` | crop_key, crop, season, need_mm_low, need_mm_high, root_depth_m_low, root_depth_m_high, depletion_p, critical_stage, region, basis (reported/derived/assumption), is_synthetic, note, source_url, root_source_url | `data/crop_water_need.csv` (hand-entered from cited sources, like villages.csv; values used unchanged) |
| `prices` | commodity, district, market, date, modal_price, reference_price, reference_type, reference_season, pct_vs_reference, source, source_url, is_synthetic | Agmarknet + official MSP (PIB) |
| `price_summary` | commodity, district, latest_date, latest_modal_price, reference_price, reference_type, reference_season, reference_source_url, pct_vs_reference, n_markets, note, source, source_url, is_synthetic | From prices; read by `get_prices`. Empty price + note when no mandi data (MSP only) |
| `livestock_prices` | item_key, item, unit, region, period, value_low, value_high, value_mid, basis (reported/derived/assumption), is_synthetic, note, source_url | `data/raw/livestock_prices.csv` (news reports + labelled assumptions) |
| `outcomes` | outcome_id, hh_id, season, decision, predicted_signal, predicted_confidence, actual_result(good/partial/loss), cattle_sold, distress_sale, request_id, rain_deficit_bucket, crop_key, note, is_synthetic | Synthetic + real (POST /api/outcomes) |
| `season_rain` | village_id, season, rain_deficit_bucket, source, is_synthetic | Rain context of past seasons (SYNTHETIC for synthetic seasons) |
| `crop_mix_keys` | decision, crop_key | Crop-mix name → canonical crop key |
| `feedback` | feedback_id, request_id, feature, context_bucket, rating(up/down), note, created_at | App (insert only) |
| `term_review` | review_id, term, meaning, category, canonical, status(pending/approved/rejected), request_id, created_at | App (insert only; latest row per term wins) |
| `alerts` | alert_id, plot_id, hh_id, stress_level, next_step, text_mr, text_hi, fact_ids, data_as_of, request_id, created_at, via(pubsub/local) | App |
| `decision_log` | request_id, hh_id, feature, facts_used, output_json, verifier_status, orchestrator_used, fallback_reason, latency_ms | App |
| `calibration_v` (view) | context_bucket (rain_deficit_bucket 0–15/15–30/>30 % × irrigation × crop_key), confidence_level, hit_rate, n, n_real, n_synthetic | From outcomes; same SQL on Postgres and BigQuery (`db/bq_calibration_v.sql`) |

---

## 6. Feature specifications

### F1: Household Risk Profile (interview agent)  [BUILD]
**Goal:** complete household profile with at most 3 follow-up questions. **User:** NGO field agent with the farmer.
**Input:** text or audio (Hindi/Marathi/English), e.g. "3 acre rented, soybean, 2 cows, well dried in August, loan due in Dec". Audio is sent to Gemini on Vertex as multimodal input.
**Behaviour:**
1. Extract the `households` fields; mark each `known | unknown`.
2. Ask up to 3 questions that most change risk confidence. Priority: water source/hours → fodder weeks → cash buffer/loan month → tenure.
3. Normalise local terms using `config/terms.yaml` first (jowar→sorghum, tur→pigeon pea, kali mitti→black cotton/vertisol), then Gemini for unknown terms.
4. Build `profile_text_norm` and embed it with the Vertex embedding model.
**Output:**
```json
{ "hh_id": "...", "profile": {...}, "unknown_fields": ["fodder_weeks_left"],
  "questions_asked": ["..."], "completeness_pct": 85 }
```
**Acceptance:** ≤ 3 questions; completeness < 60% caps downstream confidence at LOW with a visible reason; 3 Hindi and 3 Marathi samples produce valid schema; consent must be true to save.
**Refinements (P3):**
- Stateless API: `POST /api/f1/interview {history:[{role,text,field?}], lang}` returns `{type:"question", question, field_targeted, completeness_pct, profile_so_far, trace}` or `{type:"final", profile, unknown_fields, completeness_pct, trace}`. No server-side session.
- Question priority: water source/hours → fodder weeks → cash buffer/loan month → tenure, skipping known fields; max 3; asked in the user's language.
- Never guess: each extracted field needs the farmer's own words as evidence (checked in code); unstated fields stay null and are listed in `unknown_fields`. Rainfed irrigation derives `water_hours_per_week = 0` (shown in `profile.derived`).
- `config/terms.yaml` normalises local terms before Gemini; terms Gemini had to interpret are logged.
- Soil type in the farmer's words (e.g. काळी माती → black cotton soil) is stored as `soil_type_farmer` when stated; it is never asked as one of the 3 questions and never inferred.
- Ranges: acres 0–50, water_hours_per_week 0–168, cattle 0–50, goats 0–200, fodder_weeks_left 0–52; out of range → unknown and asked.
- completeness_pct = weighted share (irrigation 20, water_hours 15, fodder 15, cash 15, crops 10, cattle 10, tenure 10, acres 5; `config/thresholds.yaml`). Below 60 → `profile.flags` has "incomplete"; F3/F4 cap confidence at LOW ("profile incomplete").
- Save: `POST /api/households {profile, village_id, consent}`; consent must be true, village must exist; profile_text_norm embedded with EMBED_MODEL and saved in ONE insert with `embed_model`; hh_id `HH-NEW-nnn`, is_synthetic=false; no plot is created, so F3/F4 report "crop health (NDVI) unavailable for this household" and lower confidence one level.
- Audio: `POST /api/f1/audio` as a file upload (multipart field `file`, plus `history` JSON text, `lang`) or JSON `{audio_b64, mime_type, history, lang}` (webm/ogg/wav/mp3, ≤ 2 MB) → Gemini transcript + the same response; no speech → 422, Gemini failure → 502; the text path is unaffected.
**AI innovation / USP:** an interview that chooses its questions; profiles the cultivating household (tenants, women) rather than the landowner.

### F2: Season Risk Watch + Pub/Sub alert  [BUILD light]
**Goal:** spot stress early with no farmer effort.
**Behaviour:**
1. Rule layer computes facts: cumulative rain deficit %, longest dry spell (days), NDVI as % of normal.
2. If a threshold is crossed, the planner chooses `WAIT_NEXT_PASS | REQUEST_PHOTOS | REQUEST_DRONE | ALERT_AGENT`.
3. Gemini writes a 2-line Marathi/Hindi alert citing fact_ids.
4. The alert is published to Pub/Sub topic `stress-alerts`; the push endpoint stores it in `alerts`; the UI shows it on the household card.
**Thresholds (assumptions in `config/thresholds.yaml`):** deficit > 25%, dry spell > 21 days, NDVI < 80% of normal.
**Acceptance:** one demo plot moves to HIGH on real CHIRPS + NDVI series; the alert arrives through Pub/Sub on GCP; every alert cites ≥ 1 fact_id.
**USP:** the agent decides whether it needs more evidence; plot and household level, not district.
**KPI:** alert lead time (days before loss would be visible).

### F3: Water and Cash Crop-Mix Plan (rabi)  [BUILD, core demo]
**Goal:** compare 3 rabi crop mixes for this household under its water and cash limits.
**ADK flow:**
1. **Planner agent** chooses tools: always `get_rain_status`, `find_similar_households`, `get_calibration`, `get_soil_status`, `get_water_budget`; adds `get_prices` if cash buffer is low; adds `get_distress_probability` if `PMODEL_MODE != off`.
   **Water budget (code, P7):** per option, `coverage_pct = water available / crop water need × 100`, where water available = plant-available root-zone water now (SMAP root-zone moisture above wilting point × root depth) + irrigation over the season (water hours × pump flow ÷ area; pump flow is a labelled assumption unless the farmer states it). Need comes from `crop_water_need` (low–high range → coverage range). Missing input → coverage "not available", and the option's water_fit confidence is lowered with a data gap; numbers are never filled in.
2. **Assessor agent** receives facts only (no score) and returns per option (e.g. chana only / chana + safflower / jowar + fodder): water_fit (fit/tight/short, judged from the water-budget coverage fact; bands in `config/thresholds.yaml`), cash_at_risk (low/med/high), failure_risk (0–100), drivers[] each with fact_id, signal ACT/WAIT/AVOID, confidence HIGH/MED/LOW (must use the calibration fact).
3. **Safety floor (code):** water = short AND cash buffer = low or unknown → signal cannot be ACT; show "Safety rule applied".
   P7: when the code water budget is short (whole coverage range below the tight band), the option cannot be ACT for any cash band.
   **Missing mandi price:** if a crop has no mandi data (e.g. safflower, off-season), `get_prices` returns only the MSP fact ("MSP ₹7,215/qtl (RMS 2027-28); current mandi price unavailable"). The assessor lowers cash-risk confidence for options with that crop by one level and says why in drivers.
4. **Verifier:** code check (every fact_id exists; numbers match tool values within tolerance; water_fit consistent with the coverage band when coverage is available) + verifier agent (no unsupported claims, no product or brand pushing). On fail: one regeneration, then downgrade to LOW.
**Output:**
```json
{ "options": [ { "name":"Chana + safflower", "signal":"ACT", "failure_risk":32,
    "drivers":[{"text":"2 of 3 similar households had good results","fact_id":"F-peer-03"}],
    "water_fit":"fit", "cash_at_risk":"low", "confidence":"MED" } ],
  "recommended":"Chana + safflower", "verifier_status":"PASS", "safety_rule_applied":false,
  "explanation_mr":"...", "explanation_hi":"...", "trace":[...], "orchestrator_used":"adk" }
```
**Acceptance:** live agent trace in UI; same input gives same signal in 3 runs (temperature 0); a corrupted fact is caught by the verifier (demo it).
**USP:** Gemini judges risk rather than narrating a score; verified; confidence calibrated by outcomes; farm-level version of the government's district water-budget planning.
**KPIs:** water-short options avoided (%); verifier catch rate on injected errors; repeatability.

### F4: Livestock and Fodder Decision  [BUILD, USP]
**Goal:** avoid distress sale of cattle in drought.
**Behaviour:**
1. Facts: fodder weeks left, cost to buy fodder for N weeks, current vs normal cattle price, rain status, off-farm income. Fodder and cattle facts come from `livestock_prices` by item_key (`get_livestock_price_facts`): fact_id, value_mid, range, unit, period, source_url. Rows with basis=assumption show the SYNTHETIC badge. Fodder camp rates are from 2019: the agent must say "2019 rate; current rate not confirmed". Intake defaults to `fodder_intake_large_animal_day` (18 kg/animal/day) unless the household profile gives its own.
2. Assessor compares `KEEP_AND_BUY_FODDER | SELL_ONE_KEEP_ONE | SELL_NOW`, with cash needed and asset value kept/lost.
3. Support pointers only from `config/support_options.yaml` (hand-checked; e.g. fodder camps, MGNREGA timing). No invented links.
4. Same verifier.
**Acceptance:** demo household (3 weeks fodder, low cash) gets a reasoned option; every ₹ figure traces to a fact_id; SYNTHETIC badge on mocked prices.
**USP:** crop, livestock and labour treated as one household portfolio; targets distress asset sales documented in research.
**KPIs:** distress-sale value avoided per household (simulated ₹); share of households offered a viable non-sell option.

### P4 refinements (F4, F7, F2, explainability, learning)
- All arithmetic (₹, kg, weeks, %, dry-spell days, thresholds) is computed in code inside tools and returned as facts; Gemini compares and judges. Every number in the output must match a fact (±1%); every claim cites a valid fact_id; on fail one regeneration, then LOW. All writes are single inserts.
- **F4:** `get_livestock_facts(hh_id)`: shortfall_weeks = max(0, fodder_horizon_weeks − fodder_weeks_left); fodder cost = animals × 18 kg × 7 × shortfall × kadba ₹/kg (low/mid/high); sale value now = normal (SYNTHETIC) × sourced 20–25% drop; per-option cash needed / value kept / sale proceeds. Options: 0 cattle → not applicable (no LLM); 1 → KEEP_AND_BUY_FODDER | KEEP_WITH_SUPPORT | SELL_NOW; ≥ 2 → KEEP_AND_BUY_FODDER | SELL_ONE_KEEP_REST | SELL_NOW. Code rules: SELL_NOW only recommended when its risk is strictly lowest; no moneylender borrowing; support only from `config/support_options.yaml`; incomplete profile or unknown fodder weeks → LOW. `POST /api/f4/{hh_id}`.
- **F7:** per-option context; code rule after the assessor: HIGH and hit_rate < 0.70 and n ≥ 5 → MED ("calibration downgrade"); n < 5 → no downgrade, "insufficient history (n=X)". `GET /api/calibration`.
- **Explainability (F3, F4):** why_recommended, why_not_others (verified like drivers), what_would_change (nearest threshold computed in code), confidence_reason, data_gaps, plain_explanation_mr/hi (3 sentences, numbers checked), evidence line.
- **Learning loop:** `POST /api/outcomes` (real outcomes → calibration_v), peer outcome facts (F-peer-outcome-*), `POST /api/feedback` + top-2 "up" explanations as style examples only, `GET /api/feedback/review`, `GET/POST /api/terms/review` (approved terms join the F1 glossary).
- **F2:** `get_plot_stress_facts(plot_id)`; level in code (HIGH ≥ 2 crossed, MED 1, LOW 0); planner picks next_step (REQUEST_DRONE only when HIGH); 2-line Marathi + Hindi alert citing fact_ids and "rain data up to <date>", verified; dedupe same plot + level within 7 days; `services/alerts.py` LocalAlerts now, PubSubAlerts in P6. `POST /api/f2/{plot_id}`, `POST /api/f2/scan`, `GET /api/alerts?hh_id=`, `POST /api/alerts/receive` (Pub/Sub push envelope).

### F6: Loss Evidence Pack  [SCREEN only]
**Behaviour:** pull F2 facts (rain deficit, NDVI before/after, dates) + profile (cultivator, tenure) + optional field photo → `analyse_photo` (Gemini vision) describes visible stress → Gemini drafts a 1-page Marathi + English summary → saved to Cloud Storage.
**Acceptance:** < 20 s; every number has a source label; cultivator/tenant shown; photo findings labelled "visual assessment, not measurement".
**KPI (pitch):** days from loss to claim-ready file (target ≤ 3, within the 72-hour reporting window).

### F7: Learning and Calibration Loop  [BUILD light]
1. `calibration_v` groups outcomes by context (rain bucket × irrigation × crop) and gives hit rate per confidence level.
2. If HIGH calls in this context were right < 70% of the time (`config/thresholds.yaml`), the assessor must downgrade to MED.
3. Field-agent thumbs up/down stored; top 2 rated explanations for the context are added as few-shot examples.
**Acceptance:** demo shows a downgrade triggered by the calibration fact; outcomes labelled SYNTHETIC.
**KPI:** calibration gap = |stated confidence − observed hit rate|.

### P-Model: Distress-risk predictive model  [BUILD optional]
- Features: irrigation, water_hours, cash_buffer_band, fodder_weeks_left, off_farm_income, tenure, rain deficit bucket. Label: `distress_sale` from outcomes (SYNTHETIC).
- GCP: `CREATE MODEL ... OPTIONS(model_type='logistic_reg', model_registry='vertex_ai')` in BigQuery ML; predict with `ML.PREDICT`. Local: scikit-learn equivalent.
- Exposed only as a FACT (`F-pmodel-01: distress probability 0.62, model trained on synthetic data`). The assessor may use it; it never decides.
- Pitch honestly: "pipeline ready; retrain on real outcomes next season."

### F5, F8: Roadmap only
- **F5 Store/Sell/Keep:** cash need vs storage cost vs loan against stored grain, family food first.
- **F8 Village Resilience View:** consented aggregates + drinking water and fodder pressure for NGO/panchayat.

---

## 7. Non-functional
- Consent checkbox before saving a profile; only village-level aggregates leave the household view.
- No suicide or mental-health prediction. Livelihood and financial distress language only.
- Latency target < 15 s for F3, with step-by-step progress.
- Every request logged in `decision_log`.

## 8. Impact KPIs (pitch slide)
| KPI | Feature | How shown |
|---|---|---|
| Distress livestock sale value avoided (₹/household) | F4 | Simulation on 20–25 households |
| Water-short crop options avoided (%) | F3 | Simulation |
| Alert lead time (days) | F2 | Real 2026 CHIRPS/NDVI |
| Verifier catch rate (%) | F3/F4 | Injected-error test |
| Calibration gap | F7 | Calibration view |
| Loss → claim-ready file (days) | F6 | Demo |

---

## 9. UI / UX guidelines
**User:** NGO field agent on Android; judges on laptop. Mobile-first (360 px), good at 1280 px.
**Stack:** FastAPI serves static HTML + Tailwind (CDN) + Alpine.js or vanilla JS + Chart.js. No build step.
**Theme "Earth & Monsoon":** primary `#1B5E20`, accent `#0277BD`, soil `#6D4C41`, background `#FAFAF5`. Signals: ACT `#2E7D32`, WAIT `#F9A825`, AVOID `#C62828`, always colour + icon + word. Fonts: Noto Sans + Noto Sans Devanagari; base 16 px; signal 28 px+. Rounded cards, tap targets ≥ 48 px.
**Screens:**
1. Household list (name, village, tenure badge, risk chip, latest alert).
2. Interview F1 (mic/upload + text, ≤ 3 agent questions as chat bubbles, completeness bar, consent).
3. Season Watch F2 (rain vs normal chart, NDVI chart, stress and next-step chips, alert received via Pub/Sub).
4. Crop-mix F3 (3 option cards; recommended highlighted).
5. Livestock F4 (3 option cards: cash needed vs asset kept).
6. Evidence pack F6 (preview + download).
**Signature element: Agent Trace panel** (open by default in demo): 🧭 planner tool choices → 🛰️ facts with fact_id + source → ⚖️ assessor → ✅/❌ verifier → 📊 calibration → engine badge ("ADK on Vertex AI" / "Plain fallback"). Tapping a driver sentence highlights its fact.
**Language:** EN / मराठी / हिंदी toggle (`i18n/*.json`); explanations generated in the chosen language; read-aloud via browser `speechSynthesis`.
**Honesty markers:** SYNTHETIC badge; "Safety rule applied" banner; "Confidence lowered: missing data" note.
**States:** skeleton loaders with step text; friendly error cards; never blank.

---

## 10. Data sourcing: exact steps

### 10.1 Earth Engine
Prerequisite: register the same Cloud project for Earth Engine (non-commercial), enable the Earth Engine API, run `earthengine authenticate`.
`scripts/ee_export.py`:
1. Read `data/villages.csv` and `data/plots.csv`.
2. **Rainfall:** `UCSB-CHG/CHIRPS/DAILY`, band `precipitation`; mean over 5 km village buffer; daily from 1 Jun this year to latest available; normal = same calendar day mean 2014–2023 → `data/rain_series.csv`. CHIRPS lags by weeks: log the last available date; never fill gaps with invented values.
3. **NDVI:** `COPERNICUS/S2_SR_HARMONIZED`; 30 m plot buffer; CLOUDY_PIXEL_PERCENTAGE < 40; SCL cloud mask; NDVI = (B8 − B4)/(B8 + B4); 10-day median from 1 Jun; normal = 2019–2023 median → `data/ndvi_series.csv`.
4. **Soil moisture:** `NASA/SMAP/SPL4SMGP/008`, bands `sm_surface` (0–5 cm) and `sm_rootzone` (0–100 cm), weekly village mean → `data/soil_moisture.csv`. The water budget compares root-zone moisture with the soil's field capacity and wilting point (no multi-year normal).
4b. **Soil properties (P7):** OpenLandMap (`OpenLandMap/SOL/...`: texture class, clay, sand, organic carbon, pH, water content at 33 kPa) at the plot point, 0–30 cm mean → `data/soil_props.csv` with dataset IDs in the header. Field capacity and wilting point per texture class from FAO-56 Table 19 in `config/soil_water.yaml`; root depth and depletion fraction p per crop from FAO-56 Table 22 (in `crop_water_need`).
5. `reduceRegions` + `getInfo` in batches of ≤ 20 points. CSV header comments record dataset ID and date range.

### 10.2 Mandi prices
- Agmarknet (agmarknet.gov.in) "Market Wise Price & Arrival": Maharashtra → Dharashiv → Soybean, Tur, Bengal Gram, Jowar, Wheat (Safflower when in season) → `data/raw/agmarknet_<district>_<crop>.csv`. CEDA is unavailable.
- Official MSP in `data/raw/msp_2026.csv` (verified against PIB; values never changed): KMS 2026-27 for soybean, tur, jowar (maldandi); RMS 2027-28 for gram, safflower, wheat.
- Commodity names normalised via `config/commodities.yaml`; unknown names are logged, not guessed.
- `scripts/prep_prices.py` → `data/prices.csv` (with `pct_vs_reference` vs MSP) and `data/price_summary.csv`. A crop with no mandi data gets an MSP-only summary row; no prices are generated.
- Crop water need (P7): `data/crop_water_need.csv`, one row per crop_key and season, entered by hand from cited agronomy sources (FAO, ICAR, MPKV/VNMKV); rows without a source are basis=assumption and is_synthetic=true.
- Fodder and cattle prices: `data/raw/livestock_prices.csv` (one row per item_key, values used unchanged; basis=assumption rows must be is_synthetic=true). Replace assumption rows with survey Q6–Q7 answers when available.

### 10.3 Crop history (optional)
data.gov.in district-wise season-wise crop production, or Kaggle "Crop Production India with Weather Features" → filter Dharashiv, Latur, Beed → `data/crop_stats.csv`.

### 10.4 Households and outcomes
- `scripts/make_synthetic.py --n 25 --seed 42` → households, plots (within 3 km of villages), outcomes for 3 seasons; rules + noise documented in the script header; `is_synthetic=true`.
- `scripts/import_survey.py` maps the Google Form CSV → households (`is_synthetic=false`).
- `scripts/embed_profiles.py` builds Vertex embeddings for all households.

### 10.5 Village coordinates
Two Dharashiv villages entered by hand in `data/villages.csv` (from Google Maps). The build must not guess coordinates.

---

## 11. Build plan (~16 working hours)
| Block | Hrs | Output |
|---|---|---|
| 0. GCP project, billing (free trial), enable APIs, Vertex test call from laptop via ADC, `/health` on Cloud Run | 2 | Vertex works locally; hello-world deployed |
| 1. Earth Engine export, synthetic data, prices → Postgres | 2.5 | Tables loaded |
| 2. Tools + schemas + verifier + safety floor + **ADK engine and plain engine** + F3 + parity test | 4.5 | F3 working on both engines |
| 3. F1 interview (text + audio) + Vertex embeddings + similar households | 2 | Profiles + peers |
| 4. F4 + F7 | 1.5 | Both working |
| 5. UI per section 9 | 1.5 | Demo flow |
| 6. BigQuery backend, GCS, Pub/Sub (F2 alert), deploy to Cloud Run | 1.5 | Live URL |
| 7. Demo video + deck | 0.5 | Submission |
**P7 (after P6, before KPIs): soil + water-budget grounding (tools get_soil_status, get_water_budget; tables soil_props, crop_water_need).** Roadmap: weather outlook (forecast rain/temperature), Soil Health Card photo → N/P/K facts, fertilizer used + official prices.
**Cut order if late:** P-Model → F6 photo analysis → F7 few-shot → audio input (keep text) → Pub/Sub (keep local alerts). **Never cut:** ADK + Vertex on F3, verifier, Agent Trace.

---

## 12. Repo structure
```
seasonsaathi/
  CLAUDE.md  SPEC.md  .env.example  requirements.txt  Dockerfile  deploy.sh
  app/main.py                 # FastAPI routes + static UI + /api/alerts/receive
  app/static/                 # index.html, app.js, i18n/{en,mr,hi}.json
  config/terms.yaml thresholds.yaml support_options.yaml
  core/schemas.py llm.py verifier.py safety_floor.py
  prompts/planner.md assessor.md verifier.md interviewer.md
  tools/rain.py ndvi.py peers.py prices.py calibration.py pmodel.py photo.py soil.py water_budget.py
  orchestrators/base.py adk_engine.py plain_engine.py selector.py
  features/f1_interview.py f2_watch.py f3_cropmix.py f4_livestock.py f6_evidence.py f7_calibration.py
  repo/base.py postgres.py bigquery.py
  services/storage.py alerts.py   # local|gcs, local|pubsub
  db/schema.sql bq_schema.json bqml_pmodel.sql
  scripts/ee_export.py prep_prices.py make_synthetic.py import_survey.py embed_profiles.py load_data.py
  data/
  tests/test_verifier.py test_safety_floor.py test_f3.py test_parity.py
```

## Appendix A: Household survey (5–7 minutes; ask the main farming person; record verbal consent; note village and taluka)
1. Who mainly does the farming in your house (man / woman / both)? Is the land owned, rented or sharecropped?
2. How many acres this kharif, and which crops?
3. Water: rain only, well or borewell? If well/borewell, how many hours of water per week now?
4. What happened to this kharif crop (good / partial loss / full loss)?
5. How many cows/buffaloes/goats? How many weeks of fodder do you have now?
6. In the last 3 months, did you or anyone nearby sell an animal because of fodder or money shortage? Roughly what price vs a normal year?
7. What does fodder cost now (per tonne / bundle / load)?
8. Other income in the house (labour, MGNREGA, dairy, shop, migration)?
9. Any loan repayment due in the next 3 months (bank / SHG / moneylender)?
10. For rabi, what will you sow, or leave fallow? Why?
11. Did you claim crop insurance this year? What happened?
