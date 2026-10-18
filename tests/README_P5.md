# P5 checks (UI, SPEC section 9): manual check list

Run from the repo root with the venv active. The UI is static HTML + Tailwind CDN + vanilla JS + Chart.js, served by
FastAPI at `/` (no build step). Every API call uses a relative path, so the same files work on Cloud Run.

```bash
uvicorn app.main:app --port 8000          # then open http://localhost:8000/        (presenter mode: /?demo=1)
```
- **₹0 layout work:** `LLM_OFFLINE=true` in `.env` shows the last good result from `decision_log` (or a rule result).
- **Real answers:** `LLM_OFFLINE=false` (Lite: `GEMINI_MODEL=gemini-3.5-flash-lite`; demo: `gemini-3.5-flash`).

---

## 0. Tests
| Command | Vertex? | Expected |
|---|---|---|
| `pytest -m unit` | no | `155 passed` (P5 adds `tests/test_ui_views.py`: 10) |

## UI needs vs API has (Step 0 report, 2026-10-18)
| UI needs | API | Status |
|---|---|---|
| Household list, hub, profile | `GET /api/households`, `GET /api/households/{hh_id}` | **added in P5** (read-only: stored rows + completeness + latest F3 signal + latest alert) |
| Village picker (F1 save) | `GET /api/villages` | **added in P5** (read-only) |
| F2 rain / NDVI charts | `GET /api/plots/{plot_id}/series` | **added in P5** (stored `rain_series` / `ndvi_series` rows with `source`) |
| F4 support pointer names + links | `GET /api/support_options` | **added in P5** (`config/support_options.yaml` as-is) |
| Completeness weights, calibration thresholds | `GET /api/ui_config` | **added in P5** (`config/thresholds.yaml` as-is) |
| F3 / F4 / F1 / F2 results, trace, facts, explain | `POST /api/f3|f4/{hh_id}`, `/api/f1/interview`, `/api/f1/audio`, `/api/f2/{plot_id}`, `/api/f2/scan` | existed |
| Calibration, feedback, outcomes, terms, alerts, whoami | existing GET/POST routes | existed |
| Inject bad fact | `{"inject": "bad_fact_id"}` on F3/F4 | existed |
| Force ADK failure | `{"inject": "adk_fail"}` on F3/F4 | **added in P5** (demo flag: the selector skips ADK as failed, plain runs; `fallback_reason` says so). Button shown only when `/openapi.json` lists it |
| Impact KPIs | `/api/impact/latest` | **not available** → Impact tab hidden (feature-detected from `/openapi.json`) |
| F6 evidence pack | none (SCREEN only) | **not available** → button hidden |
| Household name | not in schema | shows `hh_id` |

---

## 1. Shell (every page)
- [ ] Header: EN / मराठी / हिंदी toggle. Choice survives a reload (localStorage). `<html lang>` changes.
- [ ] Switching language re-renders the current result **without a new API call** (same request id).
- [ ] Tabs: Households · New household · Watch · Learning · About (Impact only when `/api/impact/latest` exists).
- [ ] Footer from `/api/whoami`: AI backend + location, model, engine, data backend, storage, alerts.
- [ ] 360 px wide: no horizontal page scroll (tables scroll inside their card). Keyboard focus ring visible.
- [ ] Browser console: no errors (the only line is Tailwind's "CDN should not be used in production" warning).

## 2. Household list `#/households`
- [ ] HH-DEMO-* first with a "★ Demo" tag; SYNTHETIC or REAL badge; village · tenure; completeness bar
      (amber when below 60 %, e.g. HH-DEMO-THIN 45 %).
- [ ] Latest F3 signal chip (colour + icon + word) and alert chip when an alert exists (HH-DEMO-FLOOR: 🔔 High).

## 3. Household hub `#/hh/HH-DEMO-OK`
- [ ] Profile: unknown fields say "unknown", never 0. Rainfed households with 0 water hours show "rainfed → 0 (derived)".
- [ ] Buttons: Crop plan, Livestock, Season watch (one per plot). HH-NEW-* (no plot): "No plot location on record" banner.

## 4. F3 `#/f3/HH-DEMO-OK`, `#/f3/HH-DEMO-FLOOR`, `#/f3/HH-DEMO-THIN`
- [ ] 3 option cards: signal 28 px colour + icon + word, failure-risk bar, water fit, cash at risk, confidence,
      drivers with fact chips; the recommended card has a green ring + "★ Recommended".
- [ ] Tap a driver sentence or chip → facts table opens and the fact row flashes.
- [ ] FLOOR: "🛡️ Safety rule applied" + "📊 Calibration downgrade HIGH → MED" banners.
      THIN: "📋 Profile incomplete → confidence LOW".
- [ ] Agent Trace open by default: planner tools → tool calls with fact chips → assessor → verifier agent →
      facts table (fact_id, text, value, unit, source link, SYNTHETIC) → code verifier → calibration → safety →
      evidence line → Gemini usage → engine badge ("ADK on Vertex AI" / "Plain fallback" + reason + latency).
- [ ] Explanation: plain text in the chosen language (EN shows मराठी + हिंदी) with 🔊 Read aloud; why / why not /
      what would change (blue box) / confidence reasons / data gaps / evidence line.
- [ ] Feedback: 👍 / 👎 → "Thanks, feedback saved"; Record outcome form → "Outcome saved"; ↻ Re-run calls the API again.

## 5. F4 `#/f4/HH-DEMO-LIVESTOCK`, `#/f4/HH-DEMO-ONECOW`, `#/f4/HH-DEMO-NOCATTLE`
- [ ] LIVESTOCK: Keep all, buy fodder / Sell one, keep the rest / Sell now. Each: cash needed ₹45,360
      (range ₹38,556–₹56,700) etc. with fact chips, cash pressure vs band, risk bar, confidence, drivers, support list.
- [ ] Fodder camp support shows "⚠ 2019 rate; current rate not confirmed".
- [ ] ONECOW: options include "Keep, with support". NOCATTLE: "Not applicable – no large animals" card, no wait.

## 6. F1 `#/new`
- [ ] Paste `tests/f1_samples/mr_1.txt` → Send: chat bubbles, agent question (≤ 3), completeness bar + weights,
      profile-so-far chips, unknown fields, F1 trace (glossary terms, no-guess check, completeness).
- [ ] Mic (Chrome): record → transcript shown as "Heard: …"; on failure "Voice did not work. Use text instead."
- [ ] Final profile → pick village + tick consent → "Save and open crop plan" → F3 opens for the new HH-NEW-nnn.
      Without consent or village: "Consent and village are required".

## 7. Season watch `#/watch`, `#/watch/PLOT-HH-DEMO-FLOOR`
- [ ] Rain vs normal (mm/day, source CHIRPS) and NDVI vs normal (source Sentinel-2; gaps = no cloud-free image)
      charts; "📍 plot location SYNTHETIC".
- [ ] Check this plot → stress chip, next-step chip, "rain data up to 2026-09-30", thresholds crossed, alert MR/HI
      with fact chips and "via local/pubsub", or "Same alert already sent in the last 7 days".
- [ ] Run scan → HIGH/MED/LOW counts, alerts created, deduped, skipped list ("no plot location").

## 8. Learning `#/learning`
- [ ] Calibration table (context, confidence, hit rate, n, real, synthetic); rows HIGH with hit rate < 70 % and
      n ≥ 5 highlighted red with ⚠.
- [ ] Feedback review lists 👎 notes. Terms: pending terms with Means + Category + Approve / Reject.
- [ ] Learning demo (button on F3/F4 Feedback card, or presenter step 7): records 3 **real** `loss` outcomes for that
      request, re-runs, and shows Before / After confidence + evidence counts side by side.
      ⚠ It writes real outcome rows (`season=learning-demo-N`); use it on demo households only.

## 9. Presenter mode `/?demo=1`
- [ ] Brown bar with steps 1–9; each navigates (and forces a fresh run). Steps without an API show "(not available)".
- [ ] On F3/F4: "💉 Inject bad fact" → code verifier FAIL then "↻ Regeneration #2" in the trace (needs
      `LLM_OFFLINE=false`; offline shows the cached result).
- [ ] On F3/F4: "💥 Force ADK failure" → engine badge "Plain fallback", reason
      "adk: forced failure (demo flag inject=adk_fail)" (needs `LLM_OFFLINE=false`).

## 10. About `#/about`
- [ ] Google Cloud technology map, architecture line, real vs synthetic table.
