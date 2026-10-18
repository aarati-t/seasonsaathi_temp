# SeasonSaathi: progress and resume notes
Last updated: 2026-10-18. SPEC.md is the source of truth; this file only records where the build stands.

## Status by build block (SPEC §11)
| Block | Status |
|---|---|
| 0. GCP + Vertex via ADC + /health | Done |
| 1. Data: Earth Engine (CHIRPS, Sentinel-2, SMAP 008), Agmarknet + MSP prices, livestock prices, synthetic households → Postgres | Done |
| 2. Tools, schemas, verifier, safety floor, ADK + plain engines, F3, parity | Done |
| 3. F1 interview (text + audio, Marathi/Hindi), Vertex embeddings, similar households | Done |
| 4. F4 livestock + F7 calibration, explainability, learning loop, F2 watch | Done |
| 5. UI per SPEC §9 | Done locally 2026-10-18 (screens 1–8, 11, presenter mode `?demo=1`; checklist tests/README_P5.md). Not yet redeployed |
| 6. BigQuery, GCS, Pub/Sub, Cloud Run deploy | Done (2026-10-18) |
| P7. Soil + water-budget grounding (SPEC F3, §10.1 4b) | Code done 2026-10-18; **user runs** `ee_export.py --only soil` + `load_data.py` (tests/README_P7.md) |
| P5b. Impact simulator + Impact page (KPIs K1-K7, gaps A-F) | Code done 2026-10-18; replay works (0 model calls); **user runs** `run_impact.py --refresh` (tests/README_P5b.md) |
| 7. Demo video + deck | **Next**, after refresh + BigQuery reload + redeploy |

Tests: `pytest -q` → 178 passed, 17 skipped.

## GCP deployment (live)
- Project `migis-prototype`, region `asia-south1` (Cloud Run + BigQuery). Vertex endpoint `global`.
- URL: https://seasonsaathi-184136561293.asia-south1.run.app (revision seasonsaathi-00002-zmr)
- Service account `seasonsaathi-run@migis-prototype.iam.gserviceaccount.com`, roles: aiplatform.user,
  bigquery.dataEditor, bigquery.jobUser, pubsub.publisher, logging.logWriter; storage.objectAdmin on the bucket.
- BigQuery dataset `seasonsaathi` (loaded, `compare_counts.py` PASS). Bucket `gs://migis-prototype-seasonsaathi`.
- Pub/Sub topic `stress-alerts` + push subscription `stress-alerts-push` → `/api/alerts/receive`.
- `smoke_test.sh` passed. Steps and fixes: DEPLOY.md.
- Image excludes `.env`, `tests/`, `scripts/`, docs (`.gcloudignore`, `.dockerignore`).
- Cost when idle ≈ 0 (min-instances 0, max 2). URL is public, no login. Demo day: `MIN_INSTANCES=1 ./deploy.sh`.

## Decisions made along the way
- `GEMINI_MODEL=gemini-3.5-flash` (3.8-flash gave 429 / no quota). `EMBED_MODEL=gemini-embedding-001`.
- `GOOGLE_CLOUD_LOCATION=global` for Vertex (regional gave 404); never mix it with the Cloud Run region.
- Safety floor: unknown cash buffer counts as low; "little savings" in F1 maps to cash_buffer_band=low.
- Every model call logs backend, project, location, model, latency and tokens; the app fails fast at startup if
  GENAI_MODE=vertex but the client is not a Vertex client.
- `rain_series` / `ndvi_series` carry a `source` column (CHIRPS/Sentinel-2 vs SYNTHETIC).

## P5 additions
- Read-only routes for the UI in app/views.py: /api/households, /api/households/{id}, /api/villages,
  /api/plots/{id}/series, /api/support_options, /api/ui_config.
- Demo flag `inject: "adk_fail"` on F3/F4: selector skips ADK as failed → plain fallback with reason.
- Dev: LLM_OFFLINE=true + GEMINI_MODEL=gemini-3.5-flash-lite in .env (tests force LLM_OFFLINE=false in conftest).

## P7 additions (survey: advice must use crop, soil, water, weather)
- Tools get_soil_status, get_water_budget; tables soil_props, crop_water_need; soil_moisture.sm_rootzone;
  households.soil_type_farmer, created_at. Sources: data/crop_water_need.csv (ICAR-IIMR sorghum 425-610 mm; gram 356.5 mm
  and safflower 377 mm from Rajasthan studies - replace gram with MPKV Rahuri lysimeter value if obtained), FAO-56
  Tables 19/22 (config/soil_water.yaml). Kaggle irrigation dataset rejected (tagged synthetic, no region/source).
- Roadmap (not built): weather outlook, Soil Health Card photo -> N/P/K, fertilizer used + official prices.

## Gotchas hit
- First deploy failed: 403 `bigquery.datasets.get` → service account had no project roles. Fixed with IAM grants.
- Logs: `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="seasonsaathi"' --limit 60 --format 'value(textPayload)' | tac`

## Next session
1. User runs the P5 manual checklist (tests/README_P5.md); live checks: inject bad fact, force ADK failure, mic, F1 save.
2. KPIs (Impact tab appears automatically once `/api/impact/latest` exists) per SPEC §8 (simulated ₹ distress sale avoided, water-short options avoided, alert lead time,
   verifier catch rate, calibration gap).
3. Demo video + deck (SPEC §2 technology slide).

## How to resume
```bash
cd ~/Documents/Aarati/projects/seasonsaathi && source .venv/bin/activate
export PROJECT=migis-prototype REGION=asia-south1 SA=seasonsaathi-run@migis-prototype.iam.gserviceaccount.com
export GCS_BUCKET=$PROJECT-seasonsaathi URL=https://seasonsaathi-184136561293.asia-south1.run.app
```
Then tell Claude: "read PROGRESS.md and continue".
