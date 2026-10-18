# SeasonSaathi: rules for Claude

SPEC.md is the source of truth. Do not add features outside it.

## Scope and build order (SPEC §1, §11)
- BUILD: F1 interview, F3 crop-mix (core), F4 livestock (USP). Light: F2 watch + Pub/Sub, F7 calibration. Optional: P-Model.
- SCREEN only: F6. ROADMAP only: F5, F8, IVR/SMS. Do not build these.
- Order: 0 GCP + /health → 1 data → 2 tools/schemas/verifier/safety floor/ADK + plain engines/F3/parity → 3 F1 → 4 F4 + F7 → 5 UI → 6 BigQuery/GCS/Pub/Sub/deploy → 7 demo.
- Cut order if late: P-Model → F6 photo analysis → F7 few-shot → audio input → Pub/Sub (keep local alerts).
- Never cut: ADK + Vertex on F3, the verifier, the Agent Trace panel.

## Model access
- Gemini runs on Vertex AI (`GENAI_MODE=vertex`): ADC locally, service account on Cloud Run. AI Studio key (`GENAI_MODE=apikey`) is the fallback mode only.
- Code must run unchanged on Cloud Run with Vertex AI (service account, no keys). `GOOGLE_CLOUD_LOCATION=global` is the Vertex endpoint (current Gemini models are only served there); `CLOUD_RUN_REGION` is the Cloud Run region. Never use one for the other.
- Model names come from env (`GEMINI_MODEL`, `EMBED_MODEL`). Never hard-code them.
- Retry with exponential backoff on 429 / RESOURCE_EXHAUSTED. Cache tool results per request.

## Architecture (SPEC §3)
- ADK is the primary engine; plain engine is the fallback (auto-fallback on error, >20 s, or invalid schema).
- Both engines share tools/, prompts/, core/schemas.py, core/verifier.py, core/safety_floor.py and the decision log. Never duplicate them.
- Tools return facts only (value + fact_id + source), never decisions. Gemini judges. The verifier checks every claim cites a valid fact_id.
- All DB access goes through repo/base.py: postgres locally, bigquery on GCP (SPEC §4). Same table and column names.

## Data honesty
- Synthetic data always carries `is_synthetic=true` and a SYNTHETIC badge in the UI.
- Never invent coordinates, prices or dataset values. Never fill data gaps with made-up numbers.

## Working rules
- Never commit secrets (.env, keys, service account JSON). Keep .env.example current.
- Ask before adding any dependency not in requirements.txt.
- After each task: run `pytest`, summarise changes, tell the user what to run next.
